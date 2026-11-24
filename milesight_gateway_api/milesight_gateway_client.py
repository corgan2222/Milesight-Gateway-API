import asyncio
import base64
import json
import logging

import urllib3
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .exceptions import (
    UnexpectedResponseError,
    WebLoginRefusedError,
    WebSessionError,
)

# Disable insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# A library leaves the logging setup to the program that uses it. Errors are
# raised to the caller, so they are logged here only at debug level.
_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=30)
# The packet log keeps the last 1000 packets.
PACKET_LOG_SIZE = 1000
# The /cgi answer when the gateway no longer knows the web session.
_CGI_NO_SESSION = -2
# The web interface posts JSON as a form body; the gateway expects exactly that.
_CGI_HEADERS = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}


def _cgi_body(core, function, values):
    """Build a request body for the web interface's /cgi endpoint."""
    return json.dumps(
        {"id": 1, "execute": 1, "core": core, "function": function, "values": values}
    )


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
        self._web_cookie = None
        self._web_lock = None
        self.url_endpoint_token = f"{self.api_url}/api/internal/login"
        self.url_endpoint_devices = f"{self.api_url}/api/urdevices"
        self.url_endpoint_packet_forwarder = f"{self.api_url}/api/packet-forwarder/network-servers"
        self.url_endpoint_network_server_settings = f"{self.api_url}/api/network-server/settings"
        self.url_endpoint_applications = f"{self.api_url}/api/urapplications?organizationID=0"
        self.url_endpoint_data_transmission_integration = f"{self.api_url}/api/urapplications/{{}}/integrations/{{}}"
        self.url_endpoint_get_payload_codecs = f"{self.api_url}/api/payloadcodecs"
        self.url_endpoint_get_payload_codecs_short = f"{self.api_url}/api/payloadcodecs-short"
        self.url_endpoint_get_payload_codecs_by_device = f"{self.api_url}/api/payloadcodecs/{{}}/device"
        self.url_endpoint_get_payload_codecs_by_id = f"{self.api_url}/api/payloadcodecs/{{}}"
        self.url_endpoint_get_profiles = f"{self.api_url}/api/urprofiles"
        self.url_endpoint_get_gateway_fleet = f"{self.api_url}/api/gateways"
        self.jwt_token = None
        self.headers = None

    def encrypt_password(self, plain_text_password):
        """Encrypts the password using AES."""
        try:
            cipher = AES.new(self.secret_key, AES.MODE_CBC, self.iv)
            encrypted_password = cipher.encrypt(pad(plain_text_password.encode('utf-8'), AES.block_size))
            return base64.b64encode(encrypted_password).decode('utf-8')
        except Exception as e:
            _LOGGER.debug(f"Error encrypting password: {e}")
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
            async with session.post(self.url_endpoint_token, json=payload, headers=headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                self.jwt_token = data.get('jwt')
                self.headers = {'Authorization': f'Bearer {self.jwt_token}'}
                _LOGGER.debug("Successfully retrieved JWT token")
                return self.jwt_token
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error during JWT token retrieval: {e}")
            raise

    async def get_device(self, session: ClientSession, search: str):
        """Asynchronously fetches a device by search string."""
        try:
            async with session.get(f'{self.url_endpoint_devices}?search={search}', headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug(f"Successfully retrieved device for search: {search}")
                return data.get('deviceResult', [])
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred while fetching device: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error fetching device: {e}")
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
                async with session.get(f'{self.url_endpoint_devices}?offset={offset}&limit={limit}&applicationID=0', headers=self.headers, ssl=False) as response:
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

                    _LOGGER.debug(f"Fetched {len(devices)} devices (offset: {offset})")
            except ClientError as e:
                _LOGGER.debug(f"Client error occurred while fetching devices: {e}")
                raise
            except Exception as e:
                _LOGGER.debug(f"Error fetching devices: {e}")
                raise

        return all_devices, dev_total_count

    async def get_packet_forwarder_info(self, session: ClientSession):
        """Asynchronously fetches packet forwarder information from the gateway."""
        try:
            async with session.get(self.url_endpoint_packet_forwarder, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                servs_count = len(data.get('servs', []))
                _LOGGER.debug(f"Successfully retrieved packet forwarder info with {servs_count} servers.")
                return data, servs_count
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred while fetching packet forwarder info: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error fetching packet forwarder info: {e}")
            raise

    async def get_network_server_settings(self, session: ClientSession):
        """Asynchronously fetches network server settings from the gateway."""
        try:
            async with session.get(self.url_endpoint_network_server_settings, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug("Successfully retrieved network server settings.")
                return data
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred while fetching network server settings: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error fetching network server settings: {e}")
            raise

    async def get_all_applications(self, session: ClientSession, limit=2):
        """Asynchronously fetches all applications using pagination."""
        offset = 0
        all_applications = []
        total_count = 0

        while True:
            try:
                async with session.get(f'{self.url_endpoint_applications}&offset={offset}&limit={limit}', headers=self.headers, ssl=False) as response:
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

                    _LOGGER.debug(f"Fetched {len(applications)} applications (offset: {offset})")
            except ClientError as e:
                _LOGGER.debug(f"Client error occurred while fetching applications: {e}")
                raise
            except Exception as e:
                _LOGGER.debug(f"Error fetching applications: {e}")
                raise

        return all_applications, total_count

    async def get_data_transmission_integration(self, session: ClientSession, app_id: str, data_transmission_type: str):
        """Asynchronously fetches data transmission integration for a specific application and transmission type."""
        try:
            url = self.url_endpoint_data_transmission_integration.format(app_id, data_transmission_type)
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug(f"Successfully retrieved data transmission integration for app ID {app_id} and type {data_transmission_type}.")
                return data
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred while fetching data transmission integration: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error fetching data transmission integration: {e}")
            raise

    async def get_payload_codecs(self, session: ClientSession, codec_type: str, limit=10, search: str = None):
        """Asynchronously fetches payload codecs using pagination with an optional search parameter."""
        offset = 0
        all_codecs = []
        total_count = 0

        while True:
            try:
                # Build the URL with optional search parameter
                url = f'{self.url_endpoint_get_payload_codecs}?type={codec_type}&offset={offset}&limit={limit}'
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

                    _LOGGER.debug(f"Fetched {len(codecs)} codecs (offset: {offset}, search: {search})")
            except ClientError as e:
                _LOGGER.debug(f"Client error occurred while fetching payload codecs: {e}")
                raise
            except Exception as e:
                _LOGGER.debug(f"Error fetching payload codecs: {e}")
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

                _LOGGER.debug(f"Successfully retrieved {total_count} short payload codecs for type {codec_type}.")
                return codecs, total_count
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred while fetching payload codecs short: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error fetching payload codecs short: {e}")
            raise

    async def get_payload_codecs_by_device(self, session: ClientSession, dev_eui: str):
        """Asynchronously fetches payload codecs for a specific device."""
        try:
            url = self.url_endpoint_get_payload_codecs_by_device.format(dev_eui)
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug(f"Successfully retrieved payload codecs for device {dev_eui}.")
                return data
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred while fetching payload codecs for device {dev_eui}: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error fetching payload codecs for device {dev_eui}: {e}")
            raise

    async def get_payload_codecs_by_id(self, session: ClientSession, codec_id: str):
        """Asynchronously fetches payload codecs for a specific codec ID."""
        try:
            url = self.url_endpoint_get_payload_codecs_by_id.format(codec_id)
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug(f"Successfully retrieved payload codecs for ID {codec_id}.")
                return data
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred while fetching payload codecs for ID {codec_id}: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error fetching payload codecs for ID {codec_id}: {e}")
            raise

    async def get_profiles(self, session: ClientSession, organization_id: str, application_id: str = None, limit=10, profile_id: str = None):
        """Asynchronously fetches profiles using pagination, of one application or all."""
        offset = 0
        all_profiles = []
        total_count = 0

        while True:
            try:
                url = f'{self.url_endpoint_get_profiles}?organizationID={organization_id}&offset={offset}&limit={limit}'
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

                    _LOGGER.debug(f"Fetched {len(profiles)} profiles (offset: {offset}, profileID: {profile_id})")
            except ClientError as e:
                _LOGGER.debug(f"Client error occurred while fetching profiles: {e}")
                raise
            except Exception as e:
                _LOGGER.debug(f"Error fetching profiles: {e}")
                raise

        return all_profiles, total_count

    async def get_gateway_fleet(self, session: ClientSession, organization_id: str, search: str = None):
        """Asynchronously fetches the gateway fleet with optional search by name or gateway ID."""
        offset = 0
        limit = 20
        try:
            # Build the URL with required organizationID and optional search
            url = f'{self.url_endpoint_get_gateway_fleet}?organizationID={organization_id}&offset={offset}&limit={limit}'
            if search:
                url += f'&search={search}'
            
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                gateways = data.get('result', [])
                total_count = data.get('totalCount', 0)                

                _LOGGER.debug(f"Successfully retrieved gateway fleet for organizationID {organization_id}, search: {search}.")                
                return gateways, total_count
        except ClientError as e:
            _LOGGER.debug(f"Client error occurred while fetching gateway fleet: {e}")
            raise
        except Exception as e:
            _LOGGER.debug(f"Error fetching gateway fleet: {e}")
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
            f"{self.base_url}/islogin", timeout=REQUEST_TIMEOUT, ssl=False
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
                if self._web_cookie is None:
                    await self._web_login(session)
                result = await self._cgi(
                    session, _cgi_body(core, function, values), self._web_cookie
                )
                if result.get("status") != _CGI_NO_SESSION:
                    return result
                self._web_cookie = None
        raise WebSessionError("Gateway web interface keeps dropping the session")

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
        self._web_cookie = cookie

    async def _cgi(self, session: ClientSession, body: str, cookie):
        """Post to the web interface's /cgi endpoint and return the answer."""
        headers = {**_CGI_HEADERS, "Cookie": cookie} if cookie else _CGI_HEADERS
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
