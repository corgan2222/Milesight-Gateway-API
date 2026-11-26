"""Client for a Milesight gateway's REST API and web interface."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets

import urllib3
from aiohttp import ClientSession, ClientTimeout, FormData
from aiohttp.client_exceptions import (
    ClientError,
    ClientPayloadError,
    ServerDisconnectedError,
)
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .exceptions import (
    UnexpectedResponseError,
    WebConfigError,
    WebLoginRefusedError,
    WebSessionError,
)

# Disable insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# A library leaves the logging setup to the program that uses it. Errors are
# raised to the caller, so they are logged here only at debug level.
_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=30)
# A firmware image has tens of megabytes and may cross a slow link.
UPLOAD_TIMEOUT = ClientTimeout(total=900)
# A backup archive is far larger; anything below is an error text.
_SMALLEST_BACKUP = 512
# The packet log keeps the last 1000 packets.
PACKET_LOG_SIZE = 1000
# The /cgi answer when the gateway no longer knows the web session.
_CGI_NO_SESSION = -2
# The web interface posts JSON as a form body; the gateway expects exactly that.
# The web interface drops an idle connection after a few seconds without
# telling. A pooled one picked up later fails with "Server disconnected",
# so every web interface call closes its connection when it is done.
_CLOSE = {"Connection": "close"}
_CGI_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    **_CLOSE,
}


# Wi-Fi settings as the web interface numbers them.
WIFI_MODE_ACCESS_POINT = 0
WIFI_MODE_CLIENT = 1
WIFI_ENCRYPTION = {"open": 0, "wpa_psk": 3, "wpa2_psk": 4, "wpa_wpa2_psk": 5}
# What a peer entry asks for when it is written.
_PEER_ADD, _PEER_EDIT, _PEER_REMOVE = 1, 2, 3
WIREGUARD_INTERFACES = (1, 2, 3)


def _wireguard_index(index):
    """Check the number of a WireGuard interface; the gateway has wg0 to wg2 as 1 to 3."""
    if index not in WIREGUARD_INTERFACES:
        raise WebConfigError(
            f"WireGuard interface {index!r}; the gateway has {WIREGUARD_INTERFACES}"
        )
    return index


def _cgi_body(core, function, values):
    """Build a request body for the web interface's /cgi endpoint."""
    return json.dumps(
        {"id": 1, "execute": 1, "core": core, "function": function, "values": values}
    )


def _web_session(cookie, login_result):
    """
    Return the headers that carry a web interface session.

    Firmware up to 60.0.0.50 keeps the session in a cookie. From 60.0.0.51 on
    the gateway ignores the cookie and expects the login's username and "td"
    as a bearer token, the way its own web pages send it. Both are sent, so
    either firmware finds what it reads.
    """
    headers = {"Cookie": cookie} if cookie else {}
    try:
        first = login_result["result"][0]
        username, td = first.get("username"), first.get("td")
    except (KeyError, IndexError, TypeError, AttributeError):
        username = td = None
    if username and td:
        headers["Authorization"] = f"Bearer login={username};{td}"
    return headers


def _web_token(cookie, login_result):
    """Return the session's "td" token, which the file forms send as a field."""
    try:
        token = login_result["result"][0].get("td")
    except (KeyError, IndexError, TypeError, AttributeError):
        token = None
    if token:
        return str(token)
    for part in (cookie or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "td":
            return value
    return ""


def _summary_system(summary):
    """Return the system block of a web interface status summary, or {}."""
    try:
        blocks = summary["result"][0]["get"]
    except (KeyError, IndexError, TypeError):
        return {}
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict) and block.get("type") == "system":
            value = block.get("value")
            return value if isinstance(value, dict) else {}
    return {}


class MilesightGatewayClient:
    """Talks to one gateway: its network server API and its web interface."""

    def __init__(self, username, password, secret_key, iv, base_url, port):
        self.username = username
        self.password = password
        self.secret_key = secret_key
        self.iv = iv
        # The web interface listens on the plain address, the network server
        # API on the port.
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}:{port}"
        self.web_login_refused = False
        self._web_session = None
        self._web_token = ""
        self._web_lock = None
        self.url_endpoint_token = f"{self.api_url}/api/internal/login"
        self.url_endpoint_devices = f"{self.api_url}/api/urdevices"
        self.url_endpoint_packet_forwarder = f"{self.api_url}/api/packet-forwarder/network-servers"
        self.url_endpoint_network_server_settings = f"{self.api_url}/api/network-server/settings"
        self.url_endpoint_applications = f"{self.api_url}/api/urapplications?organizationID=0"
        self.url_endpoint_data_transmission_integration = (
            f"{self.api_url}/api/urapplications/{{}}/integrations/{{}}"
        )
        self.url_endpoint_get_payload_codecs = f"{self.api_url}/api/payloadcodecs"
        self.url_endpoint_get_payload_codecs_short = f"{self.api_url}/api/payloadcodecs-short"
        self.url_endpoint_get_payload_codecs_by_device = (
            f"{self.api_url}/api/payloadcodecs/{{}}/device"
        )
        self.url_endpoint_get_payload_codecs_by_id = f"{self.api_url}/api/payloadcodecs/{{}}"
        self.url_endpoint_get_profiles = f"{self.api_url}/api/urprofiles"
        self.url_endpoint_get_gateway_fleet = f"{self.api_url}/api/gateways"
        self.jwt_token = None
        self.headers = None

    def encrypt_password(self, plain_text_password):
        """Encrypts the password using AES."""
        try:
            cipher = AES.new(self.secret_key, AES.MODE_CBC, self.iv)
            padded = pad(plain_text_password.encode('utf-8'), AES.block_size)
            encrypted_password = cipher.encrypt(padded)
            return base64.b64encode(encrypted_password).decode('utf-8')
        except Exception as e:
            _LOGGER.debug("Error encrypting password: %s", e)
            raise

    async def get_jwt_token(self, session: ClientSession):
        """Asynchronously requests a JWT token from the gateway."""
        encrypted_password = self.encrypt_password(self.password)
        payload = {
            'username': self.username,
            'password': encrypted_password
        }

        try:
            headers = {'Content-Type': 'application/json'}
            async with session.post(
                self.url_endpoint_token, json=payload, headers=headers, ssl=False
            ) as response:
                response.raise_for_status()
                data = await response.json()
                self.jwt_token = data.get('jwt')
                self.headers = {'Authorization': f'Bearer {self.jwt_token}'}
                _LOGGER.debug("Successfully retrieved JWT token")
                return self.jwt_token
        except ClientError as e:
            _LOGGER.debug("Client error occurred: %s", e)
            raise
        except Exception as e:
            _LOGGER.debug("Error during JWT token retrieval: %s", e)
            raise

    async def get_device(self, session: ClientSession, search: str):
        """Asynchronously fetches a device by search string."""
        try:
            async with session.get(
                f'{self.url_endpoint_devices}?search={search}', headers=self.headers, ssl=False
            ) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug("Successfully retrieved device for search: %s", search)
                return data.get('deviceResult', [])
        except ClientError as e:
            _LOGGER.debug("Client error occurred while fetching device: %s", e)
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching device: %s", e)
            raise

    async def get_all_devices(self, session: ClientSession, limit=10):
        """
        Asynchronously fetches all devices using pagination.

        Paging by offset is no snapshot: on a busy network the gateway reorders
        the list between two requests, so a device can arrive on two pages and
        another on none. Duplicates are dropped here; to miss none, ask for a
        `limit` above the device count, which gives one consistent page.
        """
        offset = 0
        all_devices = []
        seen = set()
        dev_total_count = 0

        while True:
            try:
                async with session.get(
                    f'{self.url_endpoint_devices}?offset={offset}&limit={limit}&applicationID=0',
                    headers=self.headers,
                    ssl=False,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                    devices = data.get('deviceResult', [])
                    dev_total_count = data.get('devTotalCount', 0)

                    if not devices:
                        break

                    for device in devices:
                        eui = str(device.get('devEUI') or '').upper()
                        if eui and eui in seen:
                            continue
                        seen.add(eui)
                        all_devices.append(device)
                    # Go on from what arrived, not from the page size: a
                    # gateway that caps pages below `limit` would otherwise
                    # look finished after the first page.
                    offset += len(devices)

                    try:
                        total = int(dev_total_count)
                    except (TypeError, ValueError):
                        total = None
                    if total is not None:
                        if len(all_devices) >= total:
                            break
                    elif len(devices) < limit:
                        break

                    _LOGGER.debug("Fetched %s devices (offset: %s)", len(devices), offset)
            except ClientError as e:
                _LOGGER.debug("Client error occurred while fetching devices: %s", e)
                raise
            except Exception as e:
                _LOGGER.debug("Error fetching devices: %s", e)
                raise

        return all_devices, dev_total_count

    async def get_packet_forwarder_info(self, session: ClientSession):
        """Asynchronously fetches packet forwarder information from the gateway."""
        try:
            async with session.get(
                self.url_endpoint_packet_forwarder, headers=self.headers, ssl=False
            ) as response:
                response.raise_for_status()
                data = await response.json()
                servs_count = len(data.get('servs', []))
                _LOGGER.debug(
                    "Successfully retrieved packet forwarder info with %s servers.", servs_count
                )
                return data, servs_count
        except ClientError as e:
            _LOGGER.debug("Client error occurred while fetching packet forwarder info: %s", e)
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching packet forwarder info: %s", e)
            raise

    async def get_network_server_settings(self, session: ClientSession):
        """Asynchronously fetches network server settings from the gateway."""
        try:
            async with session.get(
                self.url_endpoint_network_server_settings, headers=self.headers, ssl=False
            ) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug("Successfully retrieved network server settings.")
                return data
        except ClientError as e:
            _LOGGER.debug("Client error occurred while fetching network server settings: %s", e)
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching network server settings: %s", e)
            raise

    async def get_all_applications(self, session: ClientSession, limit=2):
        """Asynchronously fetches all applications using pagination."""
        offset = 0
        all_applications = []
        total_count = 0

        while True:
            try:
                async with session.get(
                    f'{self.url_endpoint_applications}&offset={offset}&limit={limit}',
                    headers=self.headers,
                    ssl=False,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                    applications = data.get('result', [])
                    total_count = data.get('totalCount', 0)

                    if not applications:
                        break

                    all_applications.extend(applications)
                    offset += limit

                    if len(applications) < limit:
                        break

                    _LOGGER.debug("Fetched %s applications (offset: %s)", len(applications), offset)
            except ClientError as e:
                _LOGGER.debug("Client error occurred while fetching applications: %s", e)
                raise
            except Exception as e:
                _LOGGER.debug("Error fetching applications: %s", e)
                raise

        return all_applications, total_count

    async def get_data_transmission_integration(
        self, session: ClientSession, app_id: str, data_transmission_type: str
    ):
        """Fetch the data transmission integration of an application for one type."""
        try:
            url = self.url_endpoint_data_transmission_integration.format(
                app_id, data_transmission_type
            )
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug(
                    "Successfully retrieved data transmission integration "
                    "for app ID %s and type %s.",
                    app_id,
                    data_transmission_type,
                )
                return data
        except ClientError as e:
            _LOGGER.debug(
                "Client error occurred while fetching data transmission integration: %s", e
            )
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching data transmission integration: %s", e)
            raise

    async def get_payload_codecs(
        self, session: ClientSession, codec_type: str, limit=10, search: str = None
    ):
        """Fetch payload codecs page by page, optionally filtered by a search term."""
        offset = 0
        all_codecs = []
        total_count = 0

        while True:
            try:
                # Build the URL with optional search parameter
                url = (
                    f'{self.url_endpoint_get_payload_codecs}?type={codec_type}'
                    f'&offset={offset}&limit={limit}'
                )
                if search:
                    url += f'&search={search}'

                async with session.get(url, headers=self.headers, ssl=False) as response:
                    response.raise_for_status()
                    data = await response.json()
                    codecs = data.get('result', [])
                    total_count = data.get('totalCount', 0)

                    if not codecs:
                        break

                    all_codecs.extend(codecs)
                    offset += limit

                    if len(codecs) < limit:
                        break

                    _LOGGER.debug(
                        "Fetched %s codecs (offset: %s, search: %s)", len(codecs), offset, search
                    )
            except ClientError as e:
                _LOGGER.debug("Client error occurred while fetching payload codecs: %s", e)
                raise
            except Exception as e:
                _LOGGER.debug("Error fetching payload codecs: %s", e)
                raise

        return all_codecs, total_count

    async def get_payload_codecs_short(self, session: ClientSession, codec_type: str = None):
        """Asynchronously fetches payload codecs without pagination, of one type or all."""
        try:
            url = self.url_endpoint_get_payload_codecs_short
            if codec_type:
                url += f'?type={codec_type}'
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                codecs = data.get('result', [])
                total_count = data.get('totalCount', 0)

                _LOGGER.debug(
                    "Successfully retrieved %s short payload codecs for type %s.",
                    total_count,
                    codec_type,
                )
                return codecs, total_count
        except ClientError as e:
            _LOGGER.debug("Client error occurred while fetching payload codecs short: %s", e)
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching payload codecs short: %s", e)
            raise

    async def get_payload_codecs_by_device(self, session: ClientSession, dev_eui: str):
        """Asynchronously fetches payload codecs for a specific device."""
        try:
            url = self.url_endpoint_get_payload_codecs_by_device.format(dev_eui)
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug("Successfully retrieved payload codecs for device %s.", dev_eui)
                return data
        except ClientError as e:
            _LOGGER.debug(
                "Client error occurred while fetching payload codecs for device %s: %s", dev_eui, e
            )
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching payload codecs for device %s: %s", dev_eui, e)
            raise

    async def get_payload_codecs_by_id(self, session: ClientSession, codec_id: str):
        """Asynchronously fetches payload codecs for a specific codec ID."""
        try:
            url = self.url_endpoint_get_payload_codecs_by_id.format(codec_id)
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug("Successfully retrieved payload codecs for ID %s.", codec_id)
                return data
        except ClientError as e:
            _LOGGER.debug(
                "Client error occurred while fetching payload codecs for ID %s: %s", codec_id, e
            )
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching payload codecs for ID %s: %s", codec_id, e)
            raise

    async def get_profiles(
        self,
        session: ClientSession,
        organization_id: str,
        application_id: str = None,
        limit=10,
        profile_id: str = None,
    ):
        """Asynchronously fetches profiles using pagination, of one application or all."""
        offset = 0
        all_profiles = []
        total_count = 0

        while True:
            try:
                url = (
                    f'{self.url_endpoint_get_profiles}?organizationID={organization_id}'
                    f'&offset={offset}&limit={limit}'
                )
                if application_id:
                    url += f'&applicationID={application_id}'
                if profile_id:
                    url += f'&profileID={profile_id}'

                async with session.get(url, headers=self.headers, ssl=False) as response:
                    response.raise_for_status()
                    data = await response.json()
                    profiles = data.get('result', [])
                    total_count = data.get('totalCount', 0)

                    if not profiles:
                        break

                    all_profiles.extend(profiles)
                    offset += limit

                    if len(profiles) < limit:
                        break

                    _LOGGER.debug(
                        "Fetched %s profiles (offset: %s, profileID: %s)",
                        len(profiles),
                        offset,
                        profile_id,
                    )
            except ClientError as e:
                _LOGGER.debug("Client error occurred while fetching profiles: %s", e)
                raise
            except Exception as e:
                _LOGGER.debug("Error fetching profiles: %s", e)
                raise

        return all_profiles, total_count

    async def get_gateway_fleet(
        self, session: ClientSession, organization_id: str, search: str = None
    ):
        """Asynchronously fetches the gateway fleet with optional search by name or gateway ID."""
        offset = 0
        limit = 20
        try:
            # Build the URL with required organizationID and optional search
            url = (
                f'{self.url_endpoint_get_gateway_fleet}?organizationID={organization_id}'
                f'&offset={offset}&limit={limit}'
            )
            if search:
                url += f'&search={search}'

            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                gateways = data.get('result', [])
                total_count = data.get('totalCount', 0)

                _LOGGER.debug(
                    "Successfully retrieved gateway fleet for organizationID %s, search: %s.",
                    organization_id,
                    search,
                )
                return gateways, total_count
        except ClientError as e:
            _LOGGER.debug("Client error occurred while fetching gateway fleet: %s", e)
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching gateway fleet: %s", e)
            raise

    # --- network server status ------------------------------------------------
    # One request each, no pagination: these read counts and the newest
    # entries, not whole lists.

    async def _api_get(self, session: ClientSession, path: str):
        """GET a network server API path with the JWT and return the JSON object."""
        async with session.get(
            f"{self.api_url}{path}",
            headers=self.headers,
            timeout=REQUEST_TIMEOUT,
            ssl=False,
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise UnexpectedResponseError(f"{path} did not answer with an object")
        return data

    async def get_gateways(self, session: ClientSession, limit: int = 100):
        """Return the gateways the network server knows, with their link state."""
        return await self._api_get(
            session, f"/api/gateways?limit={limit}&offset=0&organizationID=0"
        )

    async def get_gateway(self, session: ClientSession, gateway_id: str):
        """
        Return what the network server knows about one gateway of its fleet.

        Besides the list's fields: wanStatus, networkIP, systemRuntime
        (seconds, as text), systemRunningStatus (a list of codes, [0] is
        normal), lastOnlineTime, lastOfflineTime and the cellular module's
        state and signal. For the gateway the network server runs on, these
        come empty.
        """
        return await self._api_get(session, f"/api/gateways/{gateway_id}")

    async def get_applications(self, session: ClientSession, limit: int = 100):
        """Return the applications with their kinds and whether they have devices."""
        return await self._api_get(
            session, f"/api/urapplications?limit={limit}&offset=0&organizationID=0"
        )

    async def get_payload_codec_settings(self, session: ClientSession):
        """Return the payload codec settings, among them the codec library version."""
        return await self._api_get(session, "/api/payloadcodecs-setting")

    async def get_packets(self, session: ClientSession, limit: int = 300):
        """Return the newest entries of the network server's packet log."""
        limit = max(1, min(limit, PACKET_LOG_SIZE))
        return await self._api_get(session, f"/api/urpackets?limit={limit}&offset=0")

    # --- downlinks ------------------------------------------------------------

    async def enqueue_downlink(
        self,
        session: ClientSession,
        dev_eui: str,
        command: dict,
        fport: int = 85,
        confirmed: bool = False,
    ):
        """
        Queue a downlink; the gateway delivers it after the device's next uplink.

        The network server runs the device's assigned encoder over the command,
        so the keys are that encoder's vocabulary, for example
        ``{"target_temperature": 21, "temperature_tolerance": 1.0}``. The queue
        wants a flat body: ChirpStack's ``deviceQueueItem`` wrapper is answered
        with 200 but queued empty.
        """
        body = {
            "confirmed": confirmed,
            "devEUI": dev_eui,
            "fPort": fport,
            "jsonObject": json.dumps(command),
        }
        await self._post_queue(session, dev_eui, body)

    async def enqueue_raw_downlink(
        self,
        session: ClientSession,
        dev_eui: str,
        data: bytes,
        fport: int = 85,
        confirmed: bool = False,
    ):
        """Queue application bytes as they are, past the device's encoder."""
        body = {
            "confirmed": confirmed,
            "devEUI": dev_eui,
            "fPort": fport,
            "data": base64.b64encode(data).decode("ascii"),
        }
        await self._post_queue(session, dev_eui, body)

    async def get_downlink_queue(self, session: ClientSession, dev_eui: str):
        """
        Return the device's pending downlinks.

        The enqueue call answers without an ID; each item here carries the
        frame counter it will be sent with.
        """
        return await self._api_get(session, f"/api/devices/{dev_eui}/queue")

    async def _post_queue(self, session: ClientSession, dev_eui: str, body: dict):
        async with session.post(
            f"{self.api_url}/api/devices/{dev_eui}/queue",
            json=body,
            headers=self.headers,
            timeout=REQUEST_TIMEOUT,
            ssl=False,
        ) as response:
            response.raise_for_status()

    # --- web interface ----------------------------------------------------------

    async def get_login_status(self, session: ClientSession):
        """
        Return the web interface's login check, which needs no session.

        Besides the login state it carries the gateway's model ("model") and
        firmware version ("rtver"); the network server API has no call for
        either.
        """
        async with session.post(
            f"{self.base_url}/islogin", headers=_CLOSE, timeout=REQUEST_TIMEOUT, ssl=False
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        return data if isinstance(data, dict) else {}

    async def get_system_info(self, session: ClientSession):
        """
        Return the system block of the web interface's status summary.

        Among others: model, sn, hardware_ver, firmware_ver, uptime (seconds),
        cpu_load and memory_avail_percent and emmc_avail_percent (text such as
        "25.20%"). An empty dict when the summary has no such block.
        """
        summary = await self.web_query(
            session, "yruo_status", "get", [{"base": "summary"}]
        )
        return _summary_system(summary)

    async def web_query(self, session: ClientSession, core: str, function: str, values: list):
        """
        Run a query against the web interface, logging in when needed.

        The web interface has its own session login, which takes the same
        account as the network server API. The session is kept and reused, and
        renewed once when the gateway no longer knows it.
        """
        if self._web_lock is None:
            self._web_lock = asyncio.Lock()
        async with self._web_lock:
            for _ in range(2):
                if self._web_session is None:
                    await self._web_login(session)
                result = await self._cgi(
                    session, _cgi_body(core, function, values), self._web_session
                )
                if result.get("status") != _CGI_NO_SESSION:
                    return result
                self._web_session = None
        raise WebSessionError("Gateway web interface keeps dropping the session")

    # --- administration through the web interface -------------------------------
    #
    # The network server API on port 8080 knows devices, codecs and packets
    # only. Wi-Fi and VPN are settings of the web interface: a "set" stores
    # them, an "apply" puts everything stored into effect, as the Save and
    # Apply buttons of the web pages do.

    async def _web_get(self, session: ClientSession, core: str):
        """Return the blocks ("type", "index", "value") the web interface keeps for a core."""
        result = await self.web_query(session, core, "get", [{"base": core}])
        try:
            blocks = result["result"][0]["get"]
        except (KeyError, IndexError, TypeError):
            blocks = None
        if result.get("status") != 0 or not isinstance(blocks, list):
            raise UnexpectedResponseError(f"The web interface returned no {core} settings")
        return [block for block in blocks if isinstance(block, dict)]

    async def _web_set(
        self, session: ClientSession, core: str, index: int, value: dict, apply: bool
    ):
        """Store values of one block, and apply them unless told otherwise."""
        if not value:
            raise WebConfigError("Nothing to set")
        result = await self.web_query(
            session, core, "set", [{"base": core, "index": index, "value": value}]
        )
        if result.get("status") != 0:
            raise WebConfigError(f"The gateway refused the {core} settings: {result.get('result')}")
        return await self.apply_web_config(session) if apply else None

    async def apply_web_config(self, session: ClientSession):
        """
        Put the stored web interface settings into effect, like the Apply button.

        Returns True when the gateway says it reboots for them. A change of
        the interface this call came in through can cut the answer off.
        """
        result = await self.web_query(session, "yruo_apply", "apply", [])
        if result.get("status") != 0:
            raise WebConfigError(
                f"The gateway could not apply the settings: {result.get('result')}"
            )
        try:
            return bool(result["result"][0].get("reboot"))
        except (KeyError, IndexError, TypeError, AttributeError):
            return False

    async def get_wifi(self, session: ClientSession):
        """
        Return the Wi-Fi settings as the web interface keeps them.

        Among others: enabled (0/1), mode (0 access point, 1 client), ssid,
        encryption (see WIFI_ENCRYPTION), key, channel, broadcast_ssid, ip and
        mask of the access point. The key comes in clear text: keep it out of
        logs.
        """
        blocks = await self._web_get(session, "yruo_wifi")
        value = blocks[0].get("value") if blocks else None
        return value if isinstance(value, dict) else {}

    async def set_wifi(self, session: ClientSession, *, apply: bool = True, **settings):
        """
        Change Wi-Fi settings by the web interface's own field names.

        Only the given fields change. With apply=False the values are stored
        and take effect with the next apply_web_config().
        """
        return await self._web_set(session, "yruo_wifi", 0, settings, apply)

    async def set_wifi_enabled(self, session: ClientSession, enabled: bool, *, apply: bool = True):
        """Switch the gateway's Wi-Fi on or off."""
        return await self.set_wifi(session, apply=apply, enabled=1 if enabled else 0)

    async def set_wifi_access_point(
        self,
        session: ClientSession,
        ssid: str,
        key: str = "",
        *,
        encryption: str = "wpa2_psk",
        enabled: bool = True,
        apply: bool = True,
    ):
        """
        Make the Wi-Fi an access point with this SSID and key.

        encryption is a name from WIFI_ENCRYPTION; "open" takes no key. An
        SSID has at most 32 bytes, a WPA key 8 to 63 characters.
        """
        if encryption not in WIFI_ENCRYPTION:
            raise WebConfigError(
                f"Unknown encryption {encryption!r}; one of {sorted(WIFI_ENCRYPTION)}"
            )
        if not ssid or len(ssid.encode("utf-8")) > 32:
            raise WebConfigError("An SSID has 1 to 32 bytes")
        settings = {
            "enabled": 1 if enabled else 0,
            "mode": WIFI_MODE_ACCESS_POINT,
            "ssid": ssid,
            "encryption": WIFI_ENCRYPTION[encryption],
        }
        if encryption != "open":
            if not 8 <= len(key) <= 63:
                raise WebConfigError("A WPA key has 8 to 63 characters")
            settings["key"] = key
        return await self.set_wifi(session, apply=apply, **settings)

    async def get_wireguard(self, session: ClientSession):
        """
        Return the WireGuard interfaces, each as a dict with its "index".

        The gateway has three (index 1 to 3, wg0 to wg2). Fields: enable,
        name, public_key, custom_private_key, private_key, address
        ("10.0.0.2/24"), port, dns, mtu and peers, each peer with id, name,
        public_key, allowed_ips (list), endpoint, port, keepalive, psk and
        route_enable. Private and preshared keys come in clear text.
        """
        blocks = await self._web_get(session, "yruo_wireguard")
        return [
            {"index": block.get("index"), **block["value"]}
            for block in blocks
            if isinstance(block.get("value"), dict)
        ]

    async def set_wireguard_interface(
        self,
        session: ClientSession,
        index: int,
        *,
        enable: bool | None = None,
        private_key: str | None = None,
        address: str | None = None,
        port: int | None = None,
        dns: str | None = None,
        mtu: int | None = None,
        apply: bool = True,
    ):
        """
        Change one WireGuard interface (index 1 to 3); None leaves a field alone.

        Without a private key of your own the gateway keeps the key pair it
        made itself; get_wireguard() tells its public key for the server. An
        empty private_key goes back to that pair.
        """
        value: dict = {}
        if enable is not None:
            value["enable"] = 1 if enable else 0
        if private_key is not None:
            value["custom_private_key"] = 1 if private_key else 0
            value["private_key"] = private_key
        for key, given in (("address", address), ("port", port), ("dns", dns), ("mtu", mtu)):
            if given is not None:
                value[key] = given
        return await self._web_set(session, "yruo_wireguard", _wireguard_index(index), value, apply)

    async def set_wireguard_peer(
        self,
        session: ClientSession,
        index: int,
        name: str,
        public_key: str,
        allowed_ips: list[str],
        *,
        endpoint: str = "",
        port: int | None = None,
        keepalive: int | None = None,
        psk: str = "",
        route_enable: bool = True,
        apply: bool = True,
    ):
        """
        Add a peer to a WireGuard interface, or replace the one of that name.

        allowed_ips are networks such as "10.0.0.0/24"; route_enable adds a
        route for each. endpoint and port name the server, keepalive is in
        seconds.
        """
        index = _wireguard_index(index)
        if not name or not public_key or not allowed_ips:
            raise WebConfigError("A peer needs a name, a public key and allowed IPs")
        interfaces = {i["index"]: i for i in await self.get_wireguard(session)}
        peers = (interfaces.get(index) or {}).get("peers") or []
        known = next((p for p in peers if isinstance(p, dict) and p.get("name") == name), None)
        peer = {
            "id": known["id"] if known and known.get("id") else secrets.token_hex(4),
            "name": name,
            "public_key": public_key,
            "allowed_ips": list(allowed_ips),
            "route_enable": 1 if route_enable else 0,
            "psk": psk,
            "endpoint": endpoint,
            "action": _PEER_EDIT if known else _PEER_ADD,
        }
        if port is not None:
            peer["port"] = port
        if keepalive is not None:
            peer["keepalive"] = keepalive
        return await self._web_set(session, "yruo_wireguard", index, {"peers": [peer]}, apply)

    async def remove_wireguard_peer(
        self, session: ClientSession, index: int, name: str, *, apply: bool = True
    ):
        """Remove the peer of that name from a WireGuard interface."""
        peer = {"name": name, "action": _PEER_REMOVE}
        return await self._web_set(
            session, "yruo_wireguard", _wireguard_index(index), {"peers": [peer]}, apply
        )

    async def download_backup(self, session: ClientSession):
        """
        Return the gateway's full configuration backup, as its Full Backup button saves it.

        The bytes can be restored on the gateway's Backup and Restore page.
        They hold the whole configuration, keys and passwords included: keep
        them like a password. Take one before every firmware upgrade.
        """
        if self._web_lock is None:
            self._web_lock = asyncio.Lock()
        async with self._web_lock:
            if self._web_session is None:
                await self._web_login(session)
            for attempt in (1, 2):
                try:
                    backup = await self._export_backup(session)
                    break
                except (ClientPayloadError, ServerDisconnectedError):
                    # Building the archive takes the gateway half a minute,
                    # and a transfer can break off. It is only read, so once more.
                    if attempt == 2:
                        raise
        # A refusal comes as a short JSON object or a login page, not as a file.
        if len(backup) < _SMALLEST_BACKUP or backup.lstrip()[:1] in (b"{", b"<"):
            raise WebConfigError("The gateway returned no backup file")
        return backup

    async def _export_backup(self, session: ClientSession) -> bytes:
        async with session.post(
            f"{self.base_url}/cgi-bin/file-export",
            data={"sessionid": self._web_token, "type": "backup", "file": "cfgbackup"},
            headers={**self._web_session, **_CLOSE},
            timeout=UPLOAD_TIMEOUT,
            ssl=False,
        ) as response:
            response.raise_for_status()
            return await response.read()

    async def upgrade_firmware(self, session: ClientSession, image: bytes, *, reset: bool = False):
        """
        Upload a firmware image; the gateway then installs it and reboots.

        This is the Upgrade button of the web interface. The gateway is gone
        for some minutes afterwards, and its HTTPS certificate may be a new
        one. reset=True also puts it back to factory settings, which removes
        its address, login and devices. An image the gateway refuses raises
        WebConfigError; nothing checks here that the image fits the model.
        """
        if not image:
            raise WebConfigError("The firmware image is empty")
        if self._web_lock is None:
            self._web_lock = asyncio.Lock()
        async with self._web_lock:
            if self._web_session is None:
                await self._web_login(session)
            form = FormData()
            form.add_field("size", str(len(image)))
            kind = "upgrade_with_reset" if reset else "upgrade"
            form.add_field("filename", f"type={kind}&file=firmware.bin")
            # Firmware up to 60.0.0.50 takes the session from this field and
            # answers "File data without session" without it.
            form.add_field("sessionid", self._web_token)
            form.add_field(
                "file", image, filename="firmware.bin", content_type="application/octet-stream"
            )
            async with session.post(
                f"{self.base_url}/cgi-bin/file-import",
                data=form,
                # Never retried: the gateway may be installing a cut-off upload.
                headers={**self._web_session, **_CLOSE},
                timeout=UPLOAD_TIMEOUT,
                ssl=False,
            ) as response:
                response.raise_for_status()
                result = await response.json(content_type=None)
        # An accepted image is answered with its size and checksum.
        if not isinstance(result, dict) or not (result.get("size") and result.get("checksum")):
            raise WebConfigError(f"The gateway refused the firmware image: {result}")
        # The gateway reboots now; the next call logs in anew.
        self._web_session = None
        return result

    async def _web_login(self, session: ClientSession):
        """Log in to the web interface and keep the session cookie."""
        if self.web_login_refused:
            raise WebLoginRefusedError("Gateway refused the web interface login earlier")
        credentials = {
            "username": self.username,
            "password": self.encrypt_password(self.password),
            "base": "web_login",
        }
        async with session.post(
            f"{self.base_url}/cgi",
            data=_cgi_body("user", "login", [credentials]),
            headers=_CGI_HEADERS,
            timeout=REQUEST_TIMEOUT,
            ssl=False,
        ) as response:
            response.raise_for_status()
            result = await response.json(content_type=None)
            cookie = "; ".join(
                f"{name}={morsel.value}" for name, morsel in response.cookies.items()
            )
        if not isinstance(result, dict) or result.get("status") != 0:
            self.web_login_refused = True
            raise WebLoginRefusedError("Gateway refused the web interface login")
        self._web_session = _web_session(cookie, result)
        self._web_token = _web_token(cookie, result)

    async def _cgi(self, session: ClientSession, body: str, web_session):
        """Post to the web interface's /cgi endpoint and return the answer."""
        headers = {**_CGI_HEADERS, **web_session} if web_session else _CGI_HEADERS
        async with session.post(
            f"{self.base_url}/cgi",
            data=body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            ssl=False,
        ) as response:
            response.raise_for_status()
            result = await response.json(content_type=None)
        if not isinstance(result, dict):
            raise UnexpectedResponseError("The web interface did not answer with an object")
        return result
