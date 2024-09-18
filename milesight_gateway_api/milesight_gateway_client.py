import base64
import logging

import urllib3
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# Disable insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MilesightGatewayClient:
    def __init__(self, username, password, secret_key, iv, base_url, port):
        self.username = username
        self.password = password
        self.secret_key = secret_key
        self.iv = iv
        self.url_endpoint_token = f"{base_url}:{port}/api/internal/login"
        self.url_endpoint_devices = f"{base_url}:{port}/api/urdevices"
        self.url_endpoint_packet_forwarder = f"{base_url}:{port}/api/packet-forwarder/network-servers"
        self.url_endpoint_network_server_settings = f"{base_url}:{port}/api/network-server/settings"
        self.url_endpoint_applications = f"{base_url}:{port}/api/urapplications?organizationID=0"
        self.url_endpoint_data_transmission_integration = f"{base_url}:{port}/api/urapplications/{{}}/integrations/{{}}"
        self.url_endpoint_get_payload_codecs = f"{base_url}:{port}/api/payloadcodecs"
        self.url_endpoint_get_payload_codecs_short = f"{base_url}:{port}/api/payloadcodecs-short"
        self.url_endpoint_get_payload_codecs_by_device = f"{base_url}:{port}/api/payloadcodecs/{{}}/device"
        self.url_endpoint_get_payload_codecs_by_id = f"{base_url}:{port}/api/payloadcodecs/{{}}"
        self.url_endpoint_get_profiles = f"{base_url}:{port}/api/urprofiles"
        self.url_endpoint_get_gateway_fleet = f"{base_url}:{port}/api/gateways"
        self.jwt_token = None
        self.headers = None

    def encrypt_password(self, plain_text_password):
        """Encrypts the password using AES."""
        try:
            cipher = AES.new(self.secret_key, AES.MODE_CBC, self.iv)
            encrypted_password = cipher.encrypt(pad(plain_text_password.encode('utf-8'), AES.block_size))
            return base64.b64encode(encrypted_password).decode('utf-8')
        except Exception as e:
            logging.error(f"Error encrypting password: {e}")
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
                logging.debug("Successfully retrieved JWT token")
                return self.jwt_token
        except ClientError as e:
            logging.error(f"Client error occurred: {e}")
            raise
        except Exception as e:
            logging.error(f"Error during JWT token retrieval: {e}")
            raise

    async def get_device(self, session: ClientSession, search: str):
        """Asynchronously fetches a device by search string."""
        try:
            async with session.get(f'{self.url_endpoint_devices}?search={search}', headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                logging.debug(f"Successfully retrieved device for search: {search}")
                return data.get('deviceResult', [])
        except ClientError as e:
            logging.error(f"Client error occurred while fetching device: {e}")
            raise
        except Exception as e:
            logging.error(f"Error fetching device: {e}")
            raise

    async def get_all_devices(self, session: ClientSession, limit=10):
        """Asynchronously fetches all devices using pagination."""
        offset = 0
        all_devices = []
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

                    all_devices.extend(devices)
                    offset += limit

                    if len(devices) < limit:
                        break

                    logging.debug(f"Fetched {len(devices)} devices (offset: {offset})")
            except ClientError as e:
                logging.error(f"Client error occurred while fetching devices: {e}")
                break
            except Exception as e:
                logging.error(f"Error fetching devices: {e}")
                break

        return all_devices, dev_total_count

    async def get_packet_forwarder_info(self, session: ClientSession):
        """Asynchronously fetches packet forwarder information from the gateway."""
        try:
            async with session.get(self.url_endpoint_packet_forwarder, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                servs_count = len(data.get('servs', []))
                logging.debug(f"Successfully retrieved packet forwarder info with {servs_count} servers.")
                return data, servs_count
        except ClientError as e:
            logging.error(f"Client error occurred while fetching packet forwarder info: {e}")
            raise
        except Exception as e:
            logging.error(f"Error fetching packet forwarder info: {e}")
            raise

    async def get_network_server_settings(self, session: ClientSession):
        """Asynchronously fetches network server settings from the gateway."""
        try:
            async with session.get(self.url_endpoint_network_server_settings, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                logging.debug("Successfully retrieved network server settings.")
                return data
        except ClientError as e:
            logging.error(f"Client error occurred while fetching network server settings: {e}")
            raise
        except Exception as e:
            logging.error(f"Error fetching network server settings: {e}")
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

                    logging.debug(f"Fetched {len(applications)} applications (offset: {offset})")
            except ClientError as e:
                logging.error(f"Client error occurred while fetching applications: {e}")
                break
            except Exception as e:
                logging.error(f"Error fetching applications: {e}")
                break

        return all_applications, total_count

    async def get_data_transmission_integration(self, session: ClientSession, app_id: str, data_transmission_type: str):
        """Asynchronously fetches data transmission integration for a specific application and transmission type."""
        try:
            url = self.url_endpoint_data_transmission_integration.format(app_id, data_transmission_type)
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                logging.debug(f"Successfully retrieved data transmission integration for app ID {app_id} and type {data_transmission_type}.")
                return data
        except ClientError as e:
            logging.error(f"Client error occurred while fetching data transmission integration: {e}")
            raise
        except Exception as e:
            logging.error(f"Error fetching data transmission integration: {e}")
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

                    logging.debug(f"Fetched {len(codecs)} codecs (offset: {offset}, search: {search})")
            except ClientError as e:
                logging.error(f"Client error occurred while fetching payload codecs: {e}")
                break
            except Exception as e:
                logging.error(f"Error fetching payload codecs: {e}")
                break

        return all_codecs, total_count

    async def get_payload_codecs_short(self, session: ClientSession, codec_type: str):
        """Asynchronously fetches payload codecs without pagination for a given type."""
        try:
            url = f'{self.url_endpoint_get_payload_codecs_short}?type={codec_type}'
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                codecs = data.get('result', [])
                total_count = data.get('totalCount', 0)

                logging.debug(f"Successfully retrieved {total_count} short payload codecs for type {codec_type}.")
                return codecs, total_count
        except ClientError as e:
            logging.error(f"Client error occurred while fetching payload codecs short: {e}")
            raise
        except Exception as e:
            logging.error(f"Error fetching payload codecs short: {e}")
            raise

    async def get_payload_codecs_by_device(self, session: ClientSession, dev_eui: str):
        """Asynchronously fetches payload codecs for a specific device."""
        try:
            url = self.url_endpoint_get_payload_codecs_by_device.format(dev_eui)
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                logging.debug(f"Successfully retrieved payload codecs for device {dev_eui}.")
                return data
        except ClientError as e:
            logging.error(f"Client error occurred while fetching payload codecs for device {dev_eui}: {e}")
            raise
        except Exception as e:
            logging.error(f"Error fetching payload codecs for device {dev_eui}: {e}")
            raise

    async def get_payload_codecs_by_id(self, session: ClientSession, codec_id: str):
        """Asynchronously fetches payload codecs for a specific codec ID."""
        try:
            url = self.url_endpoint_get_payload_codecs_by_id.format(codec_id)
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                data = await response.json()
                logging.debug(f"Successfully retrieved payload codecs for ID {codec_id}.")
                return data
        except ClientError as e:
            logging.error(f"Client error occurred while fetching payload codecs for ID {codec_id}: {e}")
            raise
        except Exception as e:
            logging.error(f"Error fetching payload codecs for ID {codec_id}: {e}")
            raise

    async def get_profiles(self, session: ClientSession, organization_id: str, application_id: str, limit=10, profile_id: str = None):
        """Asynchronously fetches profiles using pagination with optional profileID."""
        offset = 0
        all_profiles = []
        total_count = 0

        while True:
            try:
                # Build the URL with required organizationID and applicationID and optional profileID
                url = f'{self.url_endpoint_get_profiles}?organizationID={organization_id}&applicationID={application_id}&offset={offset}&limit={limit}'
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

                    logging.debug(f"Fetched {len(profiles)} profiles (offset: {offset}, profileID: {profile_id})")
            except ClientError as e:
                logging.error(f"Client error occurred while fetching profiles: {e}")
                break
            except Exception as e:
                logging.error(f"Error fetching profiles: {e}")
                break

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

                logging.info(f"Successfully retrieved gateway fleet for organizationID {organization_id}, search: {search}.")                
                return gateways, total_count
        except ClientError as e:
            logging.error(f"Client error occurred while fetching gateway fleet: {e}")
            raise
        except Exception as e:
            logging.error(f"Error fetching gateway fleet: {e}")
            raise
