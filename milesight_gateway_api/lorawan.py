"""
A Milesight gateway's network server as a backend for lorawan-connection.

Prototype. It needs Python 3.12 or newer and lorawan-connection, which this
package does not depend on: import this module only where both are present.

Inventory comes from the device list, raw uplinks from the packet log, and
downlinks go to the device queue as plain bytes. The packet log is polled,
because the MQTT feed carries decoded JSON once a device has a payload codec.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession
from lorawan_connection import (
    AckEvent,
    AddedEvent,
    ConnectionUnavailable,
    DeviceDescriptor,
    DeviceEvent,
    Downlink,
    DownlinkError,
    GatewayReception,
    RemovedEvent,
    StatusEvent,
    TxAckEvent,
    Unsubscribe,
    UpdatedEvent,
    UplinkEvent,
    notify,
    subscribe,
)

# The bundled adapters use this too; a separate package may not want to.
from lorawan_connection._subscribers import Subscribers

from . import decoder
from .exceptions import MilesightGatewayError
from .milesight_gateway_client import PACKET_LOG_SIZE, MilesightGatewayClient

_LOGGER = logging.getLogger(__name__)

MILESIGHT_EUI_PREFIX = "24e124"
# Brand and model names follow the TTN device repository, so device libraries
# can add a "milesight" entry next to their "tts" one.
MILESIGHT_BRAND = "milesight-iot"
# Suffixes the gateway's own codec copies carry after the model name.
_CODEC_SUFFIXES = ("_esec", "_new")
_CODEC_VERSION = re.compile(r"_v?\d+(\.\d+)+$")
# One request for the whole list: paging by offset is no snapshot on a busy
# network server, which reorders the list between two requests.
_ALL_DEVICES = 10000
_MAX_FAILURES = 3

Identify = Callable[[dict[str, Any]], tuple[str | None, str]]


class AuthenticationError(Exception):
    """The gateway rejected the credentials."""


@dataclass(frozen=True)
class InventoryEntry:
    """One device of the network server's list."""

    descriptor: DeviceDescriptor
    # The network server's own verdict: false once a device stopped reporting.
    active: bool
    # Inside the devices filter; only such a device gets events.
    wanted: bool


# The network server's "status" of a device that reports: 3 is one that
# stopped, past its timeout. "active" stays true for such a device.
ONLINE_STATUS = 2


def is_online(record: dict[str, Any]) -> bool:
    """Tell whether the network server hears a device; "active" where it has no status."""
    status = record.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        return status == ONLINE_STATUS
    return record.get("active") is True


def identify(record: dict[str, Any]) -> tuple[str | None, str]:
    """
    Return the brand and model ID of a device record, as far as known.

    Milesight devEUIs start with 24E124. The gateway has no model catalog, so
    the model is read from the name of the assigned payload codec, which for
    Milesight's codecs is the model (``WT101``, ``EM300-SLD_esec``), and
    written as the bundled decoder catalog writes it (``em300_sld``). A codec
    name the catalog does not know falls back to the devEUI prefix. Devices of
    other vendors stay unidentified.
    """
    eui = str(record.get("devEUI", "")).lower()
    if not eui.startswith(MILESIGHT_EUI_PREFIX):
        return None, ""
    codec = str(record.get("payloadName") or "").strip().lower().replace("-", "_")
    for suffix in _CODEC_SUFFIXES:
        if codec.endswith(suffix):
            codec = codec[: -len(suffix)]
            break
    # A codec created from the library carries its version: "AM307L-1.5.72".
    codec = _CODEC_VERSION.sub("", codec)
    if codec not in decoder.models():
        codec = decoder.model_for_eui(eui) or codec
    return MILESIGHT_BRAND, codec


def model_name(model_id: str) -> str:
    """Return the catalog's display name of a model ID, or the ID in capitals."""
    model = decoder.models().get(model_id)
    return str(model["name"]) if model else model_id.upper()


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _battery(packet: dict[str, Any]) -> float | None:
    """
    Return the battery percentage the gateway's codec decoded, or None.

    payloadJson arrives as an object or as JSON text, depending on firmware.
    """
    decoded = packet.get("payloadJson")
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except ValueError:
            return None
    if not isinstance(decoded, dict):
        return None
    value = decoded.get("battery")
    if isinstance(value, bool):
        return None
    level = _float(value)
    return level if level is not None and 0 <= level <= 100 else None


class MilesightConnection:
    """
    One gateway's network server behind one subscription.

    Callbacks are synchronous and run on the owning event loop. close() must be
    awaited by the owner. A failure marks the connection unavailable before
    notifying the owner, which can create a fresh connection. Uplinks from
    before async_connect() are not replayed.
    """

    stack_type = "milesight"

    def __init__(
        self,
        client: MilesightGatewayClient,
        session: ClientSession,
        *,
        network_id: str,
        inventory_interval: float = 300,
        packet_interval: float = 10,
        packet_window: int = 300,
        identify_device: Identify = identify,
        devices: Collection[str] | None = None,
    ) -> None:
        """
        Use an unconnected client; async_connect() logs in.

        devices limits the connection to these devEUIs; empty or None means
        every device of the network server.
        """
        self.network_id = network_id
        self._only = {eui.replace(":", "").lower() for eui in devices or ()}
        self.devices: dict[str, DeviceDescriptor] = {}
        # Every device the network server lists, also those the devices
        # filter leaves out, with whether the server counts it as active. A
        # network view shows them; no event is ever sent for one left out.
        self.inventory: dict[str, InventoryEntry] = {}
        # The newest uplink of each device, receptions included; a network
        # view reads it without subscribing.
        self.last_uplinks: dict[str, UplinkEvent] = {}
        self.available = False
        self.error: Exception | None = None
        self._client = client
        self._session = session
        self._inventory_interval = inventory_interval
        self._packet_interval = packet_interval
        self._packet_window = max(1, min(packet_window, PACKET_LOG_SIZE))
        self._identify = identify_device
        self._subscribers = Subscribers()
        self._disconnect_listeners: list[Callable[[None], None]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._seen: set[tuple[str, ...]] = set()
        # Downlinks seen leaving the gateway: devEUI, counter, time.
        self._transmitted: set[tuple[str, str, str]] = set()
        self._connecting = False
        self._closed = False

    # --- gateway calls ------------------------------------------------------

    async def _login(self) -> None:
        try:
            token = await self._client.get_jwt_token(self._session)
        except ClientResponseError as error:
            if error.status in (401, 403):
                raise AuthenticationError("The gateway rejected the login") from error
            raise
        if not token:
            raise AuthenticationError("The gateway returned no token")

    async def _call(self, method: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """Run a client call, logging in once more when the token expired."""
        try:
            return await method(self._session, *args, **kwargs)
        except ClientResponseError as error:
            if error.status not in (401, 403):
                raise
        await self._login()
        return await method(self._session, *args, **kwargs)

    # --- inventory ----------------------------------------------------------

    async def _snapshot(self) -> dict[str, DeviceDescriptor]:
        records, total = await self._call(
            self._client.get_all_devices, limit=_ALL_DEVICES
        )
        try:
            expected = int(total)
        except (TypeError, ValueError):
            expected = None
        # A short read must not look like removed devices.
        if expected is not None and len(records) < expected:
            raise ConnectionUnavailable(
                f"Incomplete device list: {len(records)} of {expected}"
            )
        snapshot: dict[str, DeviceDescriptor] = {}
        inventory: dict[str, InventoryEntry] = {}
        for record in records:
            brand, model_id = self._identify(record)
            try:
                descriptor = DeviceDescriptor(
                    self.network_id,
                    str(record.get("devEUI") or ""),
                    str(record.get("name") or record.get("devEUI")),
                    str(record.get("applicationID") or ""),
                    str(record.get("profileID") or ""),
                    model_id,
                    brand,
                    model=model_name(model_id) if model_id else "",
                    manufacturer="Milesight" if brand == MILESIGHT_BRAND else "",
                    stack=self.stack_type,
                )
            except ValueError:
                # No usable devEUI: such a record cannot carry events.
                continue
            wanted = self._wanted(descriptor.dev_eui)
            inventory[descriptor.dev_eui] = InventoryEntry(
                descriptor, active=is_online(record), wanted=wanted
            )
            if wanted:
                snapshot[descriptor.dev_eui] = descriptor
        self.inventory = inventory
        return snapshot

    def _wanted(self, eui: str) -> bool:
        return not self._only or eui in self._only

    def _emit(
        self, event: DeviceEvent, previous: DeviceDescriptor | None = None
    ) -> None:
        self._subscribers.deliver(
            event, event.descriptor or self.devices.get(event.dev_eui), previous
        )

    async def refresh(self) -> None:
        """Read the device list and announce what changed since the last read."""
        async with self._lock:
            snapshot = await self._snapshot()
            if self._closed:
                return
            old, self.devices = self.devices, snapshot
            for eui, descriptor in old.items():
                if eui not in snapshot:
                    self._emit(RemovedEvent(descriptor=descriptor))
            for eui, descriptor in snapshot.items():
                if eui not in old:
                    self._emit(AddedEvent(descriptor=descriptor))
                elif old[eui] != descriptor:
                    self._emit(UpdatedEvent(descriptor=descriptor), old[eui])

    async def async_get_gateways(self) -> list[dict[str, Any]]:
        """
        Return the gateways of the network server, a fleet hub's included.

        Each has ``id`` (the MAC, as uplink receptions name it), ``name``,
        ``connected`` (None when the server does not say) and ``last_seen``
        (the server's local time, without zone). Receptions only show
        gateways that heard a device; this lists the silent ones too.
        """
        data = await self._call(self._client.get_gateways)
        gateways = []
        for record in data.get("result") or []:
            if not isinstance(record, dict) or not record.get("mac"):
                continue
            connected = record.get("connected")
            gateways.append(
                {
                    "id": str(record["mac"]).upper(),
                    "name": str(record.get("name") or ""),
                    "connected": bool(connected)
                    if isinstance(connected, (bool, int))
                    else None,
                    "last_seen": record.get("lastSeenAt"),
                }
            )
        return gateways

    # --- uplinks ------------------------------------------------------------

    @staticmethod
    def _key(packet: dict[str, Any]) -> tuple[str, ...]:
        # Without time and gateway: one uplink heard by several gateways is
        # logged once per gateway, with the same counter and payload.
        return (
            str(packet.get("devEUI", "")).lower(),
            str(packet.get("fCnt")),
            str(packet.get("type")),
            str(packet.get("payloadBase64")),
        )

    async def _read_packets(self) -> list[dict[str, Any]]:
        log = await self._call(self._client.get_packets, limit=self._packet_window)
        return [p for p in log.get("packets") or [] if isinstance(p, dict)]

    async def _prime_packets(self) -> None:
        """Mark what the log holds now as seen, so it is not replayed."""
        packets = await self._read_packets()
        self._seen = {self._key(p) for p in packets}
        self._transmitted = {
            sent for sent in map(self._sent, packets) if sent is not None
        }

    def _acks(self, packets: list[dict[str, Any]]) -> list[AckEvent]:
        """
        Tell the ACKs of confirmed downlinks, from the packet log.

        A confirmed downlink is logged as DnCnf with its frame counter; the
        device's next uplink sets the ACK bit, logged as ack "true". The ACK
        names the queue item as async_send_downlink did: devEUI and counter.
        """
        events: list[AckEvent] = []
        last_down: dict[str, str] = {}
        told: set[tuple[str, str]] = set()
        for packet in sorted(packets, key=lambda p: str(p.get("time") or "")):
            eui = str(packet.get("devEUI", "")).lower()
            kind = str(packet.get("type", ""))
            if kind == "DnCnf":
                last_down[eui] = str(packet.get("fCnt"))
            elif (
                # The log repeats an acknowledging uplink as a row of type ACK.
                (kind.startswith("Up") or kind == "ACK")
                and str(packet.get("ack")).lower() == "true"
                and self._key(packet) not in self._seen
                and (eui, str(packet.get("fCnt"))) not in told
            ):
                try:
                    received_at = datetime.fromisoformat(str(packet.get("time")))
                except ValueError:
                    continue
                if received_at.tzinfo is None:
                    continue
                told.add((eui, str(packet.get("fCnt"))))
                events.append(
                    AckEvent(
                        network_id=self.network_id,
                        dev_eui=str(packet.get("devEUI")),
                        received_at=received_at,
                        queue_item_id=f"{eui}:{last_down.get(eui, 'queued')}",
                        acknowledged=True,
                    )
                )
        return events

    @staticmethod
    def _sent(packet: dict[str, Any]) -> tuple[str, str, str] | None:
        """
        Name a downlink that left the gateway; one still queued has no time.

        A downlink without a payload carries the network server's own MAC
        commands and is nobody's command.
        """
        if (
            not str(packet.get("type", "")).startswith("Dn")
            or not packet.get("time")
            or not packet.get("payloadBase64")
        ):
            return None
        return (
            str(packet.get("devEUI", "")).lower(),
            str(packet.get("fCnt")),
            str(packet.get("time")),
        )

    def _transmissions(self, packets: list[dict[str, Any]]) -> list[TxAckEvent]:
        """
        Tell the downlinks that left the gateway since the last poll.

        A Class A device takes a downlink right after one of its uplinks; the
        ACK of a confirmed one only comes with the uplink after that.
        """
        events: list[TxAckEvent] = []
        for packet in packets:
            sent = self._sent(packet)
            if sent is None or sent in self._transmitted:
                continue
            try:
                received_at = datetime.fromisoformat(sent[2])
                counter = int(packet.get("fCnt"))
            except (TypeError, ValueError):
                continue
            if received_at.tzinfo is None:
                continue
            events.append(
                TxAckEvent(
                    network_id=self.network_id,
                    dev_eui=str(packet.get("devEUI")),
                    received_at=received_at,
                    gateway_id=str(packet.get("gatewayMac") or ""),
                    downlink_id=counter,
                )
            )
        return events

    def _uplinks(
        self, packets: list[dict[str, Any]]
    ) -> list[UplinkEvent | StatusEvent]:
        """
        Merge the receptions of each new uplink into one event.

        An uplink whose decoded payload carries a battery percentage is
        followed by a status event with it. Milesight devices report their
        battery in the payload, not in LoRaWAN's device status answer, and a
        LoRaWAN overview reads the battery from the status.
        """
        grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for packet in packets:
            if not str(packet.get("type", "")).startswith("Up"):
                continue
            key = self._key(packet)
            if key not in self._seen:
                grouped.setdefault(key, []).append(packet)
        events: list[UplinkEvent | StatusEvent] = []
        for receptions in grouped.values():
            first = receptions[0]
            try:
                received_at = datetime.fromisoformat(str(first.get("time")))
                f_port = int(first.get("fPort"))
                data = base64.b64decode(str(first.get("payloadBase64")), validate=True)
            except (ValueError, TypeError, binascii.Error):
                continue
            # FPort 0 carries MAC commands, no application data.
            if received_at.tzinfo is None or f_port < 1:
                continue
            try:
                events.append(
                    UplinkEvent(
                        network_id=self.network_id,
                        dev_eui=str(first.get("devEUI")),
                        received_at=received_at,
                        data=data,
                        f_port=f_port,
                        receptions=tuple(
                            GatewayReception(
                                str(p.get("gatewayMac") or ""),
                                _float(p.get("rssi")),
                                _float(p.get("loraSNR")),
                            )
                            for p in receptions
                        ),
                    )
                )
            except ValueError:
                continue
            if (battery := _battery(first)) is not None:
                events.append(
                    StatusEvent(
                        network_id=self.network_id,
                        dev_eui=str(first.get("devEUI")),
                        received_at=received_at,
                        battery_level_unavailable=False,
                        battery_level=battery,
                    )
                )
        # Stable: a status stays right behind its uplink.
        return sorted(events, key=lambda event: event.received_at)

    async def poll_packets(self) -> None:
        """Deliver the uplinks the packet log gained since the last poll."""
        packets = await self._read_packets()
        # An ACK comes before the uplink that carries it, so a listener that
        # counts uplinks after a downlink already knows it arrived.
        events = sorted(
            [
                *self._acks(packets),
                *self._transmissions(packets),
                *self._uplinks(packets),
            ],
            key=lambda event: event.received_at,
        )
        self._transmitted = {
            sent for sent in map(self._sent, packets) if sent is not None
        }
        # ponytail: more than packet_window packets between two polls are
        # lost; shorten the interval or widen the window on a busy network.
        self._seen = {self._key(p) for p in packets}
        # A device outside the selection is no news; on a fleet that would
        # read the whole device list on nearly every poll.
        if any(
            event.dev_eui not in self.devices and self._wanted(event.dev_eui)
            for event in events
        ):
            await self.refresh()
        for event in events:
            if not self._closed and event.dev_eui in self.devices:
                if isinstance(event, UplinkEvent):
                    self.last_uplinks[event.dev_eui] = event
                self._emit(event)

    # --- lifecycle ----------------------------------------------------------

    async def async_connect(self) -> None:
        """Log in, read the inventory and start polling."""
        if self.available or self._connecting or self._closed:
            raise ConnectionUnavailable("Connection is closed or already started")
        self._connecting = True
        try:
            await self._login()
            await self.refresh()
            await self._prime_packets()
        except AuthenticationError:
            await self.close()
            raise
        except (ClientError, TimeoutError, MilesightGatewayError) as error:
            await self.close()
            raise ConnectionUnavailable(f"{type(error).__name__}: {error}") from error
        except BaseException:
            await self.close()
            raise
        finally:
            self._connecting = False
        if self._closed:
            raise ConnectionUnavailable("Connection closed during startup")
        self.available = True
        self._tasks = [
            asyncio.create_task(self._repeat(self.refresh, self._inventory_interval)),
            asyncio.create_task(self._repeat(self.poll_packets, self._packet_interval)),
        ]

    async def _repeat(
        self, step: Callable[[], Awaitable[None]], interval: float
    ) -> None:
        failures = 0
        while not self._closed:
            await asyncio.sleep(interval)
            try:
                await step()
            except AuthenticationError as error:
                self._failed(error)
                return
            except (
                ClientError,
                TimeoutError,
                MilesightGatewayError,
                ConnectionUnavailable,
            ) as error:
                failures += 1
                if failures >= _MAX_FAILURES:
                    self._failed(error)
                    return
                _LOGGER.warning(
                    "Milesight gateway poll failed (%s/%s): %s",
                    failures,
                    _MAX_FAILURES,
                    error,
                )
            except Exception as error:  # noqa: BLE001
                self._failed(error)
                return
            else:
                failures = 0

    async def async_subscribe(
        self,
        *,
        brands: frozenset[tuple[str, int | str]] | None = None,
        listener: Callable[[DeviceEvent], None],
    ) -> Unsubscribe:
        """Deliver matching inventory, then live events. None selects all."""
        if not self.available or self._closed:
            raise ConnectionUnavailable("Connection is not available")
        return self._subscribers.add(
            brands, listener, replay=tuple(self.devices.values())
        )

    def on_disconnect(self, listener: Callable[[], None]) -> Unsubscribe:
        """Notify after connection loss. Callers own recovery."""
        if self._closed:
            raise ConnectionUnavailable("Connection is closed")
        return subscribe(self._disconnect_listeners, lambda _: listener())

    def _failed(self, error: Exception) -> None:
        if self._closed:
            return
        self.available = False
        self._closed = True
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()
        self.error = error
        _LOGGER.warning("Milesight gateway connection failed: %s", error)
        self._subscribers.clear()
        listeners, self._disconnect_listeners = self._disconnect_listeners, []
        notify(listeners, None)

    async def close(self) -> None:
        """Stop polling. Repeated calls are harmless; closing is not a failure."""
        self.available = False
        self._closed = True
        self._subscribers.clear()
        self._disconnect_listeners.clear()
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # --- downlinks ----------------------------------------------------------

    async def async_send_downlink(self, downlink: Downlink) -> str:
        """Queue bytes for a device in the inventory and return a queue ID."""
        if not self.available or self._closed:
            raise DownlinkError("The gateway connection is not available")
        eui = downlink.dev_eui.replace(":", "").lower()
        if eui not in self.devices:
            raise DownlinkError("Device is not in the gateway's inventory")
        if downlink.expires_at is not None:
            raise DownlinkError("The Milesight queue has no expiry")
        try:
            await self._call(
                self._client.enqueue_raw_downlink,
                eui.upper(),
                downlink.data,
                fport=downlink.f_port,
                confirmed=downlink.confirmed,
            )
        except (ClientError, TimeoutError, AuthenticationError) as error:
            raise DownlinkError("The gateway did not queue the command") from error
        # The enqueue call returns no ID. The queue item's frame counter is the
        # closest stable one; without it the device EUI alone has to do.
        try:
            queue = await self._call(self._client.get_downlink_queue, eui.upper())
        except (ClientError, TimeoutError, MilesightGatewayError):
            return f"{eui}:queued"
        encoded = base64.b64encode(downlink.data).decode("ascii")
        for item in reversed(queue.get("items") or []):
            if item.get("data") == encoded and str(item.get("fPort")) == str(
                downlink.f_port
            ):
                return f"{eui}:{item.get('fCnt')}"
        return f"{eui}:queued"
