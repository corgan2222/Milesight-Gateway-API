import asyncio
import json
import sys

from aiohttp import ClientSession, ClientTimeout
from dotenv import load_dotenv

import sys
import os 

# Add project root directory to PYTHONPATH to load the class from file and not from pip
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load environment variables from .env file
load_dotenv()

from milesight_gateway_api.milesight_gateway_client import MilesightGatewayClient


# Example usage
async def main():

    # Configuration
    username = os.getenv('USERNAME')
    password = os.getenv('PASSWORD')
    secret_key = bytes(os.getenv('SECRET_KEY'), 'utf-8')  
    iv = bytes(os.getenv('IV'), 'utf-8')  
    base_url = os.getenv('BASE_URL')
    port = os.getenv('PORT')

    client = MilesightGatewayClient(username, password, secret_key, iv, base_url, port)

    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        try:
            # Step 1: Get JWT token
            await client.get_jwt_token(session)

            # Step 2: Get all devices with pagination
            all_devices, devices_total_count = await client.get_all_devices(session)
            print("All Devices from paginated request:")
            print(json.dumps(all_devices, indent=4))
            print(f"Total Devices: {devices_total_count}")

            # Step 3: Search for a specific device
            search_device_string = "6136D44340901004"
            search_device = await client.get_device(session, search_device_string)
            print(f"Search Device {search_device_string}:")
            print(json.dumps(search_device, indent=4))

            # Step 4: Get packet forwarder information
            packet_forwarder_info, servs_count = await client.get_packet_forwarder_info(session)
            print("Packet Forwarder Info:")
            print(json.dumps(packet_forwarder_info, indent=4))
            print(f"Total servs: {servs_count}")

            # Step 5: Get network server settings
            network_server_info = await client.get_network_server_settings(session)
            print("network server settings:")
            print(json.dumps(network_server_info, indent=4))
            
            # Step 6: Get network server settings
            application_infos, application_total_count = await client.get_all_applications(session)
            print("application:")
            print(json.dumps(application_infos, indent=4))
            print(f"Total applications: {application_total_count}")

            # Step 7: Get data_transmission
            app_id = 5 
            data_transmission_type = "mqtt"
            data_transmission_infos = await client.get_data_transmission_integration(session, app_id, data_transmission_type)
            print("data_transmission:")
            print(json.dumps(data_transmission_infos, indent=4))
            
            # Step 8: Get data_transmission
            # Get all payload codecs of type 'default'
            get_codec_type = "default"
            payload_codecs, codecs_total_count = await client.get_payload_codecs(session, codec_type=get_codec_type)            
            print(json.dumps(payload_codecs, indent=4))
            print(f"Total {get_codec_type} Codecs: {codecs_total_count}")
            
            # Get all payload codecs of type 'custom'
            get_codec_type = "custom"
            custom_payload_codecs, codecs_total_count = await client.get_payload_codecs(session, codec_type=get_codec_type)            
            print(json.dumps(custom_payload_codecs, indent=4))
            print(f"Total {get_codec_type} Codecs: {codecs_total_count}")
            
            # Search for specific payload codec
            get_codec_type = "custom"
            search_payload_codecs, codecs_total_count = await client.get_payload_codecs(session, codec_type='custom', search="vs350_esec")            
            print(json.dumps(search_payload_codecs, indent=4))

            # Get payload codecs (short version) of type 'default'
            codecs_short = await client.get_payload_codecs_short(session, codec_type='default')
            print("Payload Codecs Short:")
            print(json.dumps(codecs_short, indent=4))
            
            # Get payload codecs by device using devEUI
            dev_eui = "24E124707E111005"
            codecs_by_device = await client.get_payload_codecs_by_device(session, dev_eui)
            print("Payload Codecs by Device:")
            print(json.dumps(codecs_by_device, indent=4))

            # Get payload codecs by ID
            codec_id = "36"
            codec_by_id = await client.get_payload_codecs_by_id(session, codec_id)
            print(f"Payload Codecs by ID {codec_id}:")
            print(json.dumps(codec_by_id, indent=4))       

            # Get profiles with pagination and optional profileID
            organization_id = "1"
            application_id = "5"
            profile_id_search = "40b90d1d-2025-49c5-9092-6dec9bb6408f"
            profiles, total_count = await client.get_profiles(session, organization_id, application_id, profile_id=profile_id_search)
            #profiles, total_count = await client.get_profiles(session, organization_id, application_id)            
            print(f"Profiles for application_id {application_id}, profile_id {profile_id_search}  :")
            print(json.dumps(profiles, indent=4))                 
            print(f"Total Profiles: {total_count}")

            # Get gateway fleet with optional search
            organization_id = "1"
            search_term = "my-gateway"  # Optional search term
            gateway_fleet, gw_total_count = await client.get_gateway_fleet(session, organization_id)
            #gateway_fleet = await client.get_gateway_fleet(session, organization_id, search=search_term)
            print(f"Gateway Fleet  {gw_total_count}:")
            print(json.dumps(gateway_fleet, indent=4))            

        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    if sys.version_info[0] == 3 and sys.version_info[1] >= 8 and sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()    
    asyncio.set_event_loop(loop)    

    try:
        loop.run_until_complete(main())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()