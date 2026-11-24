"""
Milesight device models for lorawan-connection.

Prototype. Needs Python 3.12 or newer, lorawan-connection and dukpy. One model
class exists per catalog entry, so a collection accepts every Milesight model
the bundled decoders know, on any backend that identifies devices by the
brand ``milesight-iot`` and the catalog's model ID.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from lorawan_connection import (
    Device,
    DeviceCollection,
    DeviceDescriptor,
    DeviceEvent,
    UplinkEvent,
)

from . import decoder
from .lorawan import MILESIGHT_BRAND, MilesightConnection

_LOGGER = logging.getLogger(__name__)

# Version and identity fields every model reports; no measurement.
INFO_FIELDS = frozenset(
    {
        "ipso_version",
        "hardware_version",
        "firmware_version",
        "tsl_version",
        "sn",
        "lorawan_class",
        "reset_event",
        "device_status",
    }
)


class MilesightDevice(Device):
    """One Milesight device; holds the last decoded value of every field."""

    model_id: ClassVar[str] = ""
    # An owner on an event loop sets this and calls codec_loaded() after
    # decoder.load() ran in a worker thread.
    wait_for_codec: ClassVar[bool] = False

    def __init__(self, descriptor: DeviceDescriptor) -> None:
        """Start without values; they arrive with the first uplink."""
        super().__init__(descriptor)
        self._waiting: list[UplinkEvent] = []
        self.values: dict[str, Any] = {}
        self.last_decoded: dict[str, Any] = {}
        # Counts decoded uplinks, so listeners can tell a new report from a
        # local change: events fire only on the former.
        self.uplinks = 0

    @property
    def settings(self) -> dict[str, dict[str, Any]]:
        """Fields the encoder can set and the device reports back."""
        return self._writable("RW")

    @property
    def commands(self) -> dict[str, dict[str, Any]]:
        """Fields the encoder can send that are no state: reboot, report now."""
        return self._writable("W")

    def _writable(self, access: str) -> dict[str, dict[str, Any]]:
        if not decoder.models()[self.model_id].get("encoder"):
            return {}
        return {
            key: field
            for key, field in self.fields.items()
            if field.get("access") == access and field.get("type") != "TEXT"
        }

    @property
    def fields(self) -> dict[str, dict[str, Any]]:
        """The model's fields from the catalog, by dotted ID."""
        return decoder.models()[self.model_id]["fields"]

    @property
    def readings(self) -> dict[str, dict[str, Any]]:
        """Fields the device reports and nobody sets: its measurements."""
        return {
            key: field
            for key, field in self.fields.items()
            if field.get("access") == "R"
            and key not in INFO_FIELDS
            # The decoders draw message IDs at random; they are no state.
            and not key.endswith("msgid")
        }

    def handle_event(self, event: DeviceEvent) -> None:
        """
        Decode an uplink and keep its values.

        While wait_for_codec is set and the model's codec is still packed, the
        uplink waits for codec_loaded(): unpacking reads a file, which an
        event loop must not do.
        """
        if not isinstance(event, UplinkEvent):
            return
        if self.wait_for_codec and not decoder.is_loaded(self.model_id):
            self._waiting.append(event)
            return
        self._decode(event)

    def codec_loaded(self) -> None:
        """Decode the uplinks that waited for the codec, then tell listeners."""
        waiting, self._waiting = self._waiting, []
        for event in waiting:
            self._decode(event)
        self.notify()

    def _decode(self, event: UplinkEvent) -> None:
        try:
            decoded = decoder.decode(self.model_id, event.data)
        except decoder.DecodeError as error:
            _LOGGER.debug("Undecodable uplink from %s: %s", event.dev_eui, error)
            return
        self.last_decoded = decoded
        self.uplinks += 1
        self.values.update(decoder.flatten(decoded))
        self.notify()

    def encode(self, changes: dict[str, Any]) -> bytes:
        """
        Encode settings or commands, given by dotted field ID.

        Fields the encoder needs alongside (the catalog's ``reference``, such
        as the tolerance next to a target temperature) come from the device's
        last report, else from Milesight's example value.
        """
        payload = dict(changes)
        for key in changes:
            for ref in self.fields.get(key, {}).get("reference") or ():
                if ref in payload:
                    continue
                if ref in self.values:
                    payload[ref] = self.values[ref]
                elif "default" in self.fields.get(ref, {}):
                    payload[ref] = _coerce(
                        self.fields[ref], self.fields[ref]["default"]
                    )
        return decoder.encode(self.model_id, decoder.nest(payload))

    async def async_set(self, changes: dict[str, Any]) -> None:
        """
        Send settings or commands; keep set values until the device reports.

        Milesight devices take downlinks on FPort 85 after their next uplink.
        The send does not wait for an ACK: the Milesight network server
        reports none.
        """
        data = self.encode(changes)
        await self.async_send_downlink(data=data, f_port=85, wait_for_ack=False)
        stored = {
            key: value
            for key, value in changes.items()
            if self.fields.get(key, {}).get("access") == "RW"
        }
        if stored:
            self.values.update(stored)
            self.notify()


def _coerce(field: dict[str, Any], value: Any) -> Any:
    """Turn a catalog value (a string) into what the encoder checks for."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if field.get("type") == "NUMBER" and field.get("value_type") == "FLOAT":
        return number
    return int(number)


def _model_class(model_id: str) -> type[MilesightDevice]:
    return type(
        f"MilesightDevice_{model_id}",
        (MilesightDevice,),
        {
            "model_id": model_id,
            "identifiers": {
                MilesightConnection.stack_type: (MILESIGHT_BRAND, model_id)
            },
        },
    )


MODELS: tuple[type[MilesightDevice], ...] = tuple(
    _model_class(model_id) for model_id in decoder.models()
)


class MilesightDeviceCollection(DeviceCollection[MilesightDevice]):
    """Every Milesight model in the bundled catalog."""

    DEVICES = MODELS
