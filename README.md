# Milesight Gateway API


[![GitHub Release][release-shield]][release]
[![pip Release][pip-release-shield]][pip-release]
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/Milesight-Gateway-API)
[![License][license-shield]](LICENSE.md)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]
![Supports armhf Architecture][armhf-shield]
![Supports armv7 Architecture][armv7-shield]
![Supports i386 Architecture][i386-shield]

The **Milesight Gateway API** is a Python client that provides an interface for interacting with  Milesight Gateways through various REST API endpoints. 
It supports fetching device data, network server settings, packet forwarder information, profiles, payload codecs, and more.

## This is a beta release!

Use at your own risk.

# Why
There are some feature missing in the original firmware from Milesight, which can become a problem, especially in large installations with several gateways.

For example:
- [x] Its not possible to export the device list. [-> Fixed](https://github.com/corgan2222/Milesight-Gateway-API/blob/main/examples/export_devices.py)
- [x] Its not possible to export the custom payload codecs. [-> Fixed](https://github.com/corgan2222/Milesight-Gateway-API/blob/main/examples/export_custom_codec.py) 



# ToDo:
- [x] use all get endpoints
- [ ] use post endpoints to write on gateways
- [ ] IoT Developer Api
- [ ] state engine to use this API for receiving the actuall sensor values. 

# Requirements

The Milesight Gateway firmware must be ```60.0.0.42-r5/56.0.0.4``` or higher. (from May 28, 2024 )


## Table of Contents

- [Installation](#installation)
- [Features](#features)
- [Usage](#usage)
  - [Basic Usage](#basic-usage)
  - [Getting JWT Token](#getting-jwt-token)
  - [Fetching Devices](#fetching-devices)
  - [Fetching Applications](#fetching-applications)
  - [Fetching Payload Codecs](#fetching-payload-codecs)
  - [Fetching Profiles](#fetching-profiles)
  - [Fetching Gateway Fleet](#fetching-gateway-fleet)
- [License](#license)

## Installation

To install the package from PyPI, simply run:

```bash
pip install Milesight-Gateway-API
```

[Milesight-Gateway-API on pypi.org](https://pypi.org/project/Milesight-Gateway-API/)

# Features

The MilesightGatewayClient class provides the following features:

- Fetch Devices: Fetch devices and their details with pagination and search options.
- Fetch Network Server Settings: Get network server settings from the Milesight Gateway.
- Packet Forwarder Information: Retrieve information about the packet forwarder and connected servers.
- Fetch Applications: Retrieve all applications with pagination.
- Data Transmission Integration: Fetch integration details by ID and transmission type (HTTP or MQTT).
- Fetch Payload Codecs: Fetch default or custom payload codecs with pagination and optional search.
- Fetch Profiles: Fetch profiles for an organization and application, with optional profileID for specific content search.
- Fetch Gateway Fleet: Retrieve gateway details with optional search by name or gateway ID.

# Usage

See [/tests/test_client.py](https://github.com/corgan2222/Milesight-Gateway-API/blob/main/tests/test_client.py) how to use all functions.

## Basic Usage

First, import the MilesightGatewayClient class and configure it with your credentials. 
Ensure you have the required environment variables or load them from a .env file.
You can copy .env_eample to [.env](https://github.com/corgan2222/Milesight-Gateway-API/blob/main/examples/.env_example) and change the IP, Username and Password.


```python
from milesight_gateway_api.milesight_gateway_client import MilesightGatewayClient
import os
from aiohttp import ClientSession

# Load environment variables (e.g., USERNAME, PASSWORD, etc.)
username = os.getenv('USERNAME') # admin
password = os.getenv('PASSWORD') # the WebUI Login Password
secret_key = bytes(os.getenv('SECRET_KEY'), 'utf-8')
iv = bytes(os.getenv('IV'), 'utf-8')
base_url = os.getenv('BASE_URL')
port = os.getenv('PORT')

client = MilesightGatewayClient(username, password, secret_key, iv, base_url, port)
```

## Getting JWT Token

You must retrieve the JWT token before making other requests. Use the get_jwt_token method:

```python
async with ClientSession() as session:
    jwt_token = await client.get_jwt_token(session)    
```

## Fetching Devices

You can fetch all devices with pagination or search for a specific device by providing the search parameter.

```python
# Fetch all devices
all_devices, devices_total_count = await client.get_all_devices(session)
print(f"Total Devices: {devices_total_count}")
print(all_devices)
```

## Search for a specific device
```python
search_device_string = "device_id_here"
search_device = await client.get_device(session, search_device_string)
print(f"Search Results for {search_device_string}:")
print(search_device)
```
## Fetching Applications

To fetch all applications:

```python
# Fetch all applications with pagination
applications, total_applications = await client.get_all_applications(session)
print(f"Total Applications: {total_applications}")
print(applications)
```

## Fetching Payload Codecs

You can fetch payload codecs by type (default or custom) with optional search functionality.

```python
# Fetch all default payload codecs
payload_codecs, codecs_total_count = await client.get_payload_codecs(session, codec_type='default')
print(f"Total Payload Codecs: {codecs_total_count}")
print(payload_codecs)
```

## Search for a custom payload codec

```python
search_payload_codecs, codecs_total_count = await client.get_payload_codecs(session, codec_type='custom', search="custom_codec_name")
print(f"Search Results: {search_payload_codecs}")
```

## Fetching Profiles

You can fetch profiles for an organization and application, with an optional profile_id to search for specific profile content.

```python
# Fetch profiles
organization_id = "1"
application_id = "5"
profiles, total_profiles = await client.get_profiles(session, organization_id, application_id)
print(f"Total Profiles: {total_profiles}")
print(profiles)
```

```python
# Fetch specific profile by profileID
profile_id = "your_profile_id"
profiles_by_id, total_profiles = await client.get_profiles(session, organization_id, application_id, profile_id=profile_id)
print(f"Profiles for profileID {profile_id}: {profiles_by_id}")
```

## Fetching Gateway Fleet

Fetch the gateway fleet with optional search by gateway name or ID.

```python
# Fetch gateway fleet
organization_id = "1"
gateway_fleet, total_gateways = await client.get_gateway_fleet(session, organization_id)
print(f"Total Gateways: {total_gateways}")
print(gateway_fleet)
```

## Search for a gateway by name or ID

```python
gateway_fleet_search, total_gateways = await client.get_gateway_fleet(session, organization_id, search="gateway_name_or_id")
print(f"Search Results: {gateway_fleet_search}")
```

# Usefull Links

- [Test Rest API with Postman](https://support.milesight-iot.com/support/solutions/articles/73000514150-how-to-test-milesight-gateway-http-api-by-postman-)
- [Milesight API Documentation](https://support.milesight-iot.com/helpdesk/attachments/73117065743)
- [Milesight-Gateway-API on pypi.org](https://pypi.org/project/Milesight-Gateway-API/)


# License

This project is licensed under the MIT License. See the LICENSE file for more details.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
[armhf-shield]: https://img.shields.io/badge/armv7-yes-green.svg
[armv7-shield]: https://img.shields.io/badge/armv7-yes-green.svg
[i386-shield]: https://img.shields.io/badge/i386-no-red.svg
[release-shield]: https://img.shields.io/github/v/release/corgan2222/Milesight-Gateway-API
[release]:        https://github.com/corgan2222/Milesight-Gateway-API/releases
[pip-release-shield]: https://img.shields.io/pypi/v/Milesight-Gateway-API
[pip-release]:        https://pypi.org/project/Milesight-Gateway-API/
[license-shield]: https://img.shields.io/github/license/corgan2222/Milesight-Gateway-API