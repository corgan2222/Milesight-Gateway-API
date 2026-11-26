# Milesight Gateway API

[![GitHub Release][release-shield]][release]
[![PyPI][pip-release-shield]][pip-release]
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/Milesight-Gateway-API)
[![License][license-shield]](LICENSE)

A Python client for Milesight LoRaWAN gateways (UG65, UG67 and their
relatives). It talks to the gateway's local REST API and web interface, and
decodes uplinks with Milesight's own codecs. The
[Home Assistant integration](https://github.com/corgan2222/ha-milesight-gateway)
is built on it.

- **Network server**: devices, applications, payload codecs, profiles, gateways of a fleet, packet log
- **Downlinks**: queue a command for the gateway's codec, or raw bytes
- **Web interface**: model, firmware, serial number, CPU, memory, storage, uptime
- **Firmware**: the newest release from Milesight's download page
- **Decoder** (extra): Milesight's decoders and encoders for 145 models, with a field catalog per model
- **LoRaWAN connection** (Python 3.12+): a [lorawan-connection](https://pypi.org/project/lorawan-connection/) backend on top of the packet log

It also covers what the gateway's own interface lacks on large installations,
such as exporting the [device list](examples/export_devices.py), the
[gateways](examples/export_gateways.py) or the
[custom payload codecs](examples/export_custom_codec.py).

## Installation

```bash
pip install Milesight-Gateway-API            # client
pip install "Milesight-Gateway-API[decoder]" # client and decoder
```

The gateway needs firmware `60.0.0.42-r5` or newer.

## Usage

The client takes the gateway login and the AES Secret Key and IV from the
gateway's security settings. Copy [examples/.env_example](examples/.env_example)
to `.env` for the examples.

```python
import os
from aiohttp import ClientSession
from milesight_gateway_api import MilesightGatewayClient

client = MilesightGatewayClient(
    os.getenv("USERNAME"),
    os.getenv("PASSWORD"),
    os.getenv("SECRET_KEY").encode(),  # 16 characters
    os.getenv("IV").encode(),          # 16 characters
    os.getenv("BASE_URL"),             # https://192.0.2.10
    os.getenv("PORT"),                 # 8080
)

async with ClientSession() as session:
    await client.get_jwt_token(session)                     # log in first
    # One page above the device count gives a consistent list on a busy network.
    devices, total = await client.get_all_devices(session, limit=10000)
    device = await client.get_device(session, "24E1247140000001")
```

### Network server

| Call | Returns |
| ---- | ------- |
| `get_all_devices(session)`, `get_device(session, search)` | Devices, all or by name or devEUI |
| `get_all_applications(session)`, `get_applications(session)` | Applications |
| `get_data_transmission_integration(session, app_id, "mqtt")` | An application's MQTT or HTTP integration |
| `get_payload_codecs(session, "default" \| "custom", search=...)` | Payload codecs |
| `get_payload_codecs_by_device(session, dev_eui)` | The codec assigned to a device |
| `get_profiles(session, organization_id, application_id)` | Device profiles |
| `get_gateway_fleet(session, organization_id)`, `get_gateways(session)` | The gateways of a fleet with their link state |
| `get_packets(session, limit)` | The newest entries of the packet log |
| `get_network_server_settings(session)`, `get_packet_forwarder_info(session)` | Network server and packet forwarder |

### Downlinks

The gateway runs the device's codec over the command, so the keys are that
codec's vocabulary. The device picks the downlink up after its next uplink.

```python
await client.enqueue_downlink(session, "24E1247140000001", {"report_interval": 10})
await client.enqueue_raw_downlink(session, "24E1247140000001", bytes.fromhex("ff2800"), 85)
queue = await client.get_downlink_queue(session, "24E1247140000001")
```

### Web interface and firmware

The web interface has its own session with the same account. After one refused
login the client stops trying and raises `WebLoginRefusedError`, because the
gateway locks an account after a few failed logins.

```python
from milesight_gateway_api import get_latest_firmware

info = await client.get_login_status(session)   # model, firmware; no login needed
system = await client.get_system_info(session)  # serial, uptime, CPU, memory
release = await get_latest_firmware(session, "60")  # 60: UG65 and UG67
print(release.version, release.download_url)
```

### Wi-Fi and WireGuard

Both are settings of the web interface. A call stores its values and applies
them, like the page's Save and Apply buttons; `apply=False` only stores, and
`apply_web_config()` then puts everything stored into effect at once.

```python
wifi = await client.get_wifi(session)                # enabled, mode, ssid, encryption, key
await client.set_wifi_enabled(session, False)        # access point off
await client.set_wifi_access_point(session, "Workshop", "a-long-key")  # WPA2, on

interfaces = await client.get_wireguard(session)     # wg0 to wg2 as index 1 to 3
print(interfaces[0]["public_key"])                   # for the server's peer entry
await client.set_wireguard_interface(
    session, 1, enable=True, address="10.0.0.2/24", apply=False
)
await client.set_wireguard_peer(
    session, 1, "server", "<server public key>", ["10.0.0.0/24"],
    endpoint="vpn.example.com", port=51820, keepalive=25, apply=False,
)
await client.apply_web_config(session)
await client.remove_wireguard_peer(session, 1, "server")
```

`upgrade_firmware(session, image)` uploads a firmware image the way the
Upgrade button does; the gateway installs it and reboots. `reset=True` also
puts it back to factory settings.

Keys come back in clear text from `get_wifi()` and `get_wireguard()`. A value
the gateway refuses raises `WebConfigError`.

## Decoder

With the `decoder` extra, the package runs Milesight's own decoders and
encoders from [Milesight-IoT/codec](https://github.com/Milesight-IoT/codec)
(MIT) in an embedded JavaScript engine ([dukpy](https://pypi.org/project/dukpy/)),
so raw bytes decode exactly as on the gateway. The scripts ship packed; a
model's script is unpacked on first use.

```python
from milesight_gateway_api import decoder

decoder.decode("ws203", bytes.fromhex("01754003674400046871050000"))
# {'battery': 64, 'temperature': 6.8, 'humidity': 56.5, 'occupancy': 0}

decoder.models()["ws203"]["fields"]["occupancy"]  # type, unit, access, value names

decoder.encode("wt101", {"report_status": 1})      # b'\xff\x28\x00', for FPort 85
decoder.encode("wt101", decoder.nest({"temperature_control.enable": 0}))
```

Enumerations come back as raw numbers; the model's `fields` name them.
`scripts/update_codecs.py` refreshes the bundle from Milesight's repository.

## LoRaWAN connection

`milesight_gateway_api.lorawan` implements the lorawan-connection protocol on
top of the network server's packet log: devices, uplinks with their gateway
receptions, raw downlinks and the fleet's gateways. It needs Python 3.12 or
newer and `pip install lorawan-connection`.

```python
from lorawan_connection import UplinkEvent
from milesight_gateway_api.lorawan import MilesightConnection

connection = MilesightConnection(client, session, network_id="home")
await connection.async_connect()

def listener(event):
    if isinstance(event, UplinkEvent):
        print(event.dev_eui, event.data.hex(), [rx.rssi for rx in event.receptions])

unsubscribe = await connection.async_subscribe(listener=listener)
gateways = await connection.async_get_gateways()
...
await connection.close()
```

`milesight_gateway_api.lorawan_devices` builds on it: devices that decode their
uplinks with the bundled codecs and encode settings.

## Links

- [Milesight API documentation](https://support.milesight-iot.com/helpdesk/attachments/73117065743)
- [Testing the gateway API with Postman](https://support.milesight-iot.com/support/solutions/articles/73000514150-how-to-test-milesight-gateway-http-api-by-postman-)
- [Milesight-Gateway-API on PyPI](https://pypi.org/project/Milesight-Gateway-API/)

## License

MIT, see [LICENSE](LICENSE). The bundled codecs are Milesight's, under their
own MIT license in `milesight_gateway_api/codecs/LICENSE`.

[release-shield]: https://img.shields.io/github/v/release/corgan2222/Milesight-Gateway-API
[release]: https://github.com/corgan2222/Milesight-Gateway-API/releases
[pip-release-shield]: https://img.shields.io/pypi/v/Milesight-Gateway-API
[pip-release]: https://pypi.org/project/Milesight-Gateway-API/
[license-shield]: https://img.shields.io/github/license/corgan2222/Milesight-Gateway-API
