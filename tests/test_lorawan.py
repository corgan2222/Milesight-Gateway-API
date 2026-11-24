"""Offline tests for the lorawan-connection backend.

Skipped where lorawan-connection is not installed; it needs Python 3.12.
"""

import asyncio
import base64

import pytest

pytest.importorskip("lorawan_connection")

# pylint: disable=wrong-import-position,import-error
from datetime import UTC, datetime

from conftest import FakeResponse
from lorawan_connection import (
    AddedEvent,
    ConnectionUnavailable,
    Downlink,
    DownlinkError,
    RemovedEvent,
    UplinkEvent,
)

from milesight_gateway_api import MilesightGatewayClient
from milesight_gateway_api.lorawan import (
    AuthenticationError,
    MilesightConnection,
    identify,
)

GATEWAY = "http://192.0.2.10"
API = f"{GATEWAY}:8080"
LOGIN = ("POST", f"{API}/api/internal/login")
DEVICES = ("GET", f"{API}/api/urdevices?offset=0&limit=10000&applicationID=0")
PACKETS = ("GET", f"{API}/api/urpackets?limit=300&offset=0")

THERMOSTAT = {
    "devEUI": "24E1247140000001",
    "name": "radiator",
    "applicationID": "5",
    "profileID": "p1",
    "payloadName": "WT101",
}
FOREIGN = {
    "devEUI": "2CF7F12000000003",
    "name": "foreign",
    "applicationID": "5",
    "profileID": "p2",
    "payloadName": "S2101",
}


def _devices(*records):
    return FakeResponse({"deviceResult": list(records), "devTotalCount": len(records)})


def _uplink(eui, fcnt, data, *, gateway="24E124FFFE000001", rssi="-90", time=None):
    return {
        "devEUI": eui,
        "fCnt": fcnt,
        "type": "UpUnc",
        "fPort": "85",
        "payloadBase64": base64.b64encode(data).decode(),
        "time": time or "2026-11-24T12:59:29+01:00",
        "gatewayMac": gateway,
        "rssi": rssi,
        "loraSNR": "9.5",
    }


def _client():
    return MilesightGatewayClient(
        "admin", "secret", b"1111111111111111", b"2222222222222222", GATEWAY, 8080
    )


@pytest.fixture
async def connection(session):
    session.routes[LOGIN] = FakeResponse({"jwt": "a.jwt.token"})
    session.routes[DEVICES] = _devices(THERMOSTAT, FOREIGN)
    session.routes[PACKETS] = FakeResponse({"packets": [_uplink(THERMOSTAT["devEUI"], 1, b"\x01")]})
    conn = MilesightConnection(
        _client(), session, network_id="net", inventory_interval=3600, packet_interval=3600
    )
    await conn.async_connect()
    yield conn
    await conn.close()


async def _events(conn):
    events = []
    await conn.async_subscribe(listener=events.append)
    return events


def test_milesight_devices_are_identified_by_eui_and_codec():
    """Brand from the devEUI prefix, model from the codec name without suffix."""
    assert identify(THERMOSTAT) == ("milesight-iot", "wt101")
    assert identify({**THERMOSTAT, "payloadName": "VS321_esec"}) == ("milesight-iot", "vs321")
    assert identify({**THERMOSTAT, "payloadName": "EM300-SLD"}) == (
        "milesight-iot",
        "em300_sld",
    )
    assert identify(FOREIGN) == (None, "")


def test_an_unknown_codec_name_falls_back_to_the_eui_prefix():
    """A home-made codec name says nothing; the devEUI still names the model."""
    assert identify({**THERMOSTAT, "payloadName": "my-radiator"}) == (
        "milesight-iot",
        "wt101",
    )


def test_an_uplink_updates_the_device_values():
    """The device model decodes its uplinks and tells its listeners."""
    pytest.importorskip("dukpy")
    from lorawan_connection import DeviceDescriptor

    from milesight_gateway_api.lorawan_devices import (
        MilesightDeviceCollection,
    )

    (model,) = [m for m in MilesightDeviceCollection.DEVICES if m.model_id == "ws203"]
    assert model.identifiers == {"milesight": ("milesight-iot", "ws203")}
    device = model(
        DeviceDescriptor(
            "net", "24E1247910000003", "room", "5", "p", "ws203", "milesight-iot",
            stack="milesight",
        )
    )
    updates = []
    device.add_update_listener(lambda: updates.append(True))

    device._receive_event(
        UplinkEvent(
            network_id="net",
            dev_eui="24E1247910000003",
            received_at=datetime(2026, 11, 24, tzinfo=UTC),
            data=bytes.fromhex("01754003674400046871050000"),
        )
    )

    assert device.values == {"battery": 64, "temperature": 6.8, "humidity": 56.5, "occupancy": 0}
    assert updates == [True]
    assert "temperature" in device.readings
    assert "firmware_version" not in device.readings


def _device(model_id, eui):
    pytest.importorskip("dukpy")
    from lorawan_connection import DeviceDescriptor

    from milesight_gateway_api.lorawan_devices import MilesightDeviceCollection

    (model,) = [m for m in MilesightDeviceCollection.DEVICES if m.model_id == model_id]
    return model(
        DeviceDescriptor(
            "net", eui, "device", "5", "p", model_id, "milesight-iot", stack="milesight"
        )
    )


def test_an_uplink_waits_for_a_packed_codec(monkeypatch):
    """On an event loop the codec is unpacked elsewhere; the uplink waits."""
    from milesight_gateway_api import decoder

    sensor = _device("ws203", "24E1247910000003")
    monkeypatch.setattr(type(sensor), "wait_for_codec", True)
    monkeypatch.setattr(decoder, "_scripts", {})
    sensor._receive_event(
        UplinkEvent(
            network_id="net",
            dev_eui="24E1247910000003",
            received_at=datetime(2026, 11, 24, tzinfo=UTC),
            data=bytes.fromhex("01754003674400046871050000"),
        )
    )
    assert sensor.values == {}

    decoder.load(["ws203"])
    sensor.codec_loaded()

    assert sensor.values["temperature"] == 6.8


def test_random_message_ids_are_no_reading():
    """A button's msgid is drawn by the decoder at random, so no entity."""
    button = _device("ws101", "24E1245350000002")

    assert "button_event.status" in button.readings
    assert "button_event.msgid" not in button.readings


async def test_a_setting_goes_out_as_bytes_and_stays_until_reported():
    """The encoder's bytes on FPort 85; the set value shows at once."""
    thermostat = _device("wt101", "24E1247140000001")
    sent = []

    async def send(downlink):
        sent.append(downlink)
        return "queued"

    thermostat._send_downlink = send
    assert "target_temperature" in thermostat.settings
    assert "reboot" in thermostat.commands

    # No tolerance reported yet: Milesight's example value (0) fills it in.
    await thermostat.async_set({"target_temperature": 21})
    await thermostat.async_set({"report_status": 1})

    assert [(d.f_port, d.data.hex()) for d in sent] == [(85, "ffb1150000"), (85, "ff2800")]
    assert thermostat.values == {"target_temperature": 21}


async def test_inventory_is_replayed_to_a_subscriber(connection):
    """Every device arrives as ADDED, with stack and identity."""
    events = await _events(connection)

    assert [type(e) for e in events] == [AddedEvent, AddedEvent]
    thermostat = next(e.descriptor for e in events if e.descriptor.brand_id)
    assert (thermostat.stack, thermostat.brand_id, thermostat.model_id) == (
        "milesight",
        "milesight-iot",
        "wt101",
    )
    assert thermostat.dev_eui == "24e1247140000001"


async def test_brands_filter_the_subscription(connection):
    """A brand filter leaves out devices of other vendors."""
    events = []
    await connection.async_subscribe(
        brands=frozenset({("milesight", "milesight-iot")}), listener=events.append
    )

    assert [e.dev_eui for e in events] == ["24e1247140000001"]


async def test_the_log_from_before_connect_is_not_replayed(connection, session):
    """What the packet log held at startup is history, not news."""
    events = await _events(connection)
    await connection.poll_packets()

    assert not [e for e in events if isinstance(e, UplinkEvent)]


async def test_a_new_uplink_arrives_with_its_raw_bytes(connection, session):
    """Bytes, port and both gateway receptions end up in one event."""
    events = await _events(connection)
    eui = THERMOSTAT["devEUI"]
    session.routes[PACKETS] = FakeResponse(
        {
            "packets": [
                _uplink(eui, 2, b"\x03\x67\xe6\x00", gateway="GW-A", rssi="-80"),
                _uplink(eui, 2, b"\x03\x67\xe6\x00", gateway="GW-B", rssi="-100"),
                _uplink(eui, 1, b"\x01"),
            ]
        }
    )

    await connection.poll_packets()
    await connection.poll_packets()  # the same log again delivers nothing new

    (uplink,) = [e for e in events if isinstance(e, UplinkEvent)]
    assert connection.last_uplinks["24e1247140000001"] is uplink
    assert uplink.data == b"\x03\x67\xe6\x00"
    assert uplink.f_port == 85
    assert uplink.received_at == datetime(2026, 11, 24, 11, 59, 29, tzinfo=UTC)
    assert {(r.gateway_id, r.rssi) for r in uplink.receptions} == {
        ("GW-A", -80.0),
        ("GW-B", -100.0),
    }


@pytest.mark.parametrize(
    "broken",
    [
        {"fPort": "0"},
        {"payloadBase64": "not base64!"},
        {"time": "2026-11-24T12:59:29"},
        {"type": "DnUnc"},
    ],
)
async def test_unusable_packets_are_skipped(connection, session, broken):
    """MAC frames, junk, zone-less times and downlinks are not uplinks."""
    events = await _events(connection)
    packet = {**_uplink(THERMOSTAT["devEUI"], 5, b"\x09"), **broken}
    session.routes[PACKETS] = FakeResponse({"packets": [packet]})

    await connection.poll_packets()

    assert not [e for e in events if isinstance(e, UplinkEvent)]


async def test_an_uplink_of_a_new_device_reads_the_inventory_first(connection, session):
    """The descriptor reaches subscribers before the device's first uplink."""
    events = await _events(connection)
    newcomer = {**THERMOSTAT, "devEUI": "24E1247140000009", "name": "new"}
    session.routes[DEVICES] = _devices(THERMOSTAT, FOREIGN, newcomer)
    session.routes[PACKETS] = FakeResponse({"packets": [_uplink(newcomer["devEUI"], 1, b"\x02")]})

    await connection.poll_packets()

    tail = events[2:]
    assert [type(e) for e in tail] == [AddedEvent, UplinkEvent]
    assert tail[0].dev_eui == tail[1].dev_eui == "24e1247140000009"


async def test_a_selection_limits_the_devices_and_their_uplinks(session):
    """Unselected devices stay out, and their uplinks read no device list."""
    session.routes[LOGIN] = FakeResponse({"jwt": "a.jwt.token"})
    session.routes[DEVICES] = _devices(THERMOSTAT, FOREIGN)
    session.routes[PACKETS] = FakeResponse({"packets": []})
    conn = MilesightConnection(
        _client(),
        session,
        network_id="net",
        inventory_interval=3600,
        packet_interval=3600,
        devices=["24E1247140000001"],
    )
    await conn.async_connect()
    events = await _events(conn)
    session.routes[PACKETS] = FakeResponse(
        {"packets": [_uplink(FOREIGN["devEUI"], 1, b"\x01")]}
    )
    reads = sum(1 for _, url, _ in session.calls if "urdevices" in url)

    await conn.poll_packets()

    assert [e.dev_eui for e in events] == ["24e1247140000001"]
    assert sum(1 for _, url, _ in session.calls if "urdevices" in url) == reads
    await conn.close()


async def test_the_gateway_list_names_every_gateway(connection, session):
    """Silent gateways too, keyed by the MAC that receptions carry."""
    session.routes[("GET", f"{API}/api/gateways?limit=100&offset=0&organizationID=0")] = (
        FakeResponse(
            {
                "result": [
                    {"mac": "c0ba1ffffe000001", "name": "GW-1", "connected": True,
                     "lastSeenAt": "2026-11-24 14:37:47"},
                    {"mac": "C0BA1FFFFE000002", "name": "GW-2", "connected": 0},
                    {"name": "no mac"},
                ],
                "totalCount": 3,
            }
        )
    )

    gateways = await connection.async_get_gateways()

    assert [(g["id"], g["name"], g["connected"]) for g in gateways] == [
        ("C0BA1FFFFE000001", "GW-1", True),
        ("C0BA1FFFFE000002", "GW-2", False),
    ]


async def test_a_device_gone_from_the_list_is_removed(connection, session):
    """A complete list without a device announces its removal."""
    events = await _events(connection)
    session.routes[DEVICES] = _devices(THERMOSTAT)

    await connection.refresh()

    assert [type(e) for e in events[2:]] == [RemovedEvent]
    assert events[2].dev_eui == "2cf7f12000000003"


async def test_an_incomplete_list_removes_nothing(connection, session):
    """A short read is an error, not a set of removals."""
    events = await _events(connection)
    session.routes[DEVICES] = FakeResponse(
        {"deviceResult": [THERMOSTAT], "devTotalCount": 2}
    )
    session.routes[
        ("GET", f"{API}/api/urdevices?offset=1&limit=10000&applicationID=0")
    ] = FakeResponse({"deviceResult": [], "devTotalCount": 2})

    with pytest.raises(ConnectionUnavailable):
        await connection.refresh()
    assert len(events) == 2


async def test_a_downlink_goes_out_as_raw_bytes(connection, session):
    """The queue gets base64 data; the item's frame counter becomes the ID."""
    queue = ("POST", f"{API}/api/devices/24E1247140000001/queue")
    session.routes[queue] = FakeResponse({})
    session.routes[("GET", f"{API}/api/devices/24E1247140000001/queue")] = FakeResponse(
        {"items": [{"data": base64.b64encode(b"\xff\x28\x00").decode(), "fPort": 85, "fCnt": 7}]}
    )

    queue_id = await connection.async_send_downlink(
        Downlink(dev_eui="24E1247140000001", f_port=85, data=b"\xff\x28\x00")
    )

    assert queue_id == "24e1247140000001:7"
    body = next(kw["json"] for method, url, kw in session.calls if (method, url) == queue)
    assert body["data"] == base64.b64encode(b"\xff\x28\x00").decode()
    assert "jsonObject" not in body


@pytest.mark.parametrize(
    ("downlink", "reason"),
    [
        (Downlink(dev_eui="24E1249990000009", f_port=85, data=b"\x01"), "inventory"),
        (
            Downlink(
                dev_eui="24E1247140000001",
                f_port=85,
                data=b"\x01",
                expires_at=datetime(2027, 1, 1, tzinfo=UTC),
            ),
            "expiry",
        ),
    ],
)
async def test_unsupported_downlinks_are_refused(connection, downlink, reason):
    """Unknown devices and an expiry the queue cannot honour are errors."""
    with pytest.raises(DownlinkError, match=reason):
        await connection.async_send_downlink(downlink)


async def test_rejected_credentials_are_an_authentication_error(session):
    """A refused login is told apart from an outage."""
    session.routes[LOGIN] = FakeResponse(status=401)
    conn = MilesightConnection(_client(), session, network_id="net")

    with pytest.raises(AuthenticationError):
        await conn.async_connect()
    await conn.close()


async def test_an_unreachable_gateway_is_unavailable(session):
    """Any other failure at startup is a temporary outage."""
    session.routes[LOGIN] = FakeResponse({"jwt": "a.jwt.token"})
    session.routes[DEVICES] = FakeResponse(status=500)
    conn = MilesightConnection(_client(), session, network_id="net")

    with pytest.raises(ConnectionUnavailable):
        await conn.async_connect()


async def test_repeated_poll_failures_disconnect_once(session):
    """Three failed polls in a row end the connection and tell the owner."""
    session.routes[LOGIN] = FakeResponse({"jwt": "a.jwt.token"})
    session.routes[DEVICES] = _devices(THERMOSTAT)
    session.routes[PACKETS] = FakeResponse({"packets": []})
    conn = MilesightConnection(
        _client(), session, network_id="net", inventory_interval=3600, packet_interval=0
    )
    await conn.async_connect()
    lost = []
    conn.on_disconnect(lambda: lost.append(True))
    session.routes[PACKETS] = FakeResponse(status=500)

    for _ in range(20):
        await asyncio.sleep(0)
        if lost:
            break

    assert lost == [True]
    assert not conn.available
    await conn.close()
    await conn.close()  # harmless when repeated
