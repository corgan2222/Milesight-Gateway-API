import asyncio
import logging
import json
import csv
import sys
import os

import urllib3
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from dotenv import load_dotenv

from milesight_gateway_api.milesight_gateway_client import MilesightGatewayClient

# Load environment variables from .env file
load_dotenv()

# Disable insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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

            all_devices, devices_total_count = await client.get_all_devices(session)
            print("All Devices from paginated request:")
            print(json.dumps(all_devices, indent=4))
            save_devices_to_csv(all_devices)
            #print(f"Total Devices: {devices_total_count}")
            

        except Exception as e:
            print(f"Error: {e}")


def save_devices_to_csv(devices, file_path='export/devices_export.csv'):
    """
    Save the provided list of device entries to a CSV file.

    Args:
    devices (list): List of device entries (dictionaries) to be saved.
    file_path (str): Path to the CSV file to save the data.
    """
    # Define the CSV headers and corresponding keys from the JSON
    headers = [
        'name', 'description', 'devEUI', 'deviceprofile', 'application', 'payloadcodec', 
        'fport', 'appkey', 'devaddr', 'nwkskey', 'appskey'
    ]
    
    # Open the CSV file for writing
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, mode='w', newline='', encoding='utf-8') as csv_file:
        writer = csv.writer(csv_file)
        
        # Write the header
        writer.writerow(headers)
        
        # Write the data rows
        for device in devices:
            writer.writerow([
                device.get('name', '-') if device.get('name') else '-',
                device.get('description', '-') if device.get('description') else '-',
                device.get('devEUI', '-') if device.get('devEUI') else '-',
                device.get('profileName', '-') if device.get('profileName') else '-', 
                device.get('appName', '-') if device.get('appName') else '-',         
                device.get('payloadName', '-') if device.get('payloadName') else '-', 
                device.get('fPort', '-') if device.get('fPort') else '-',             
                device.get('appKey', '-') if device.get('appKey') else '-',1,"",""
                # device.get('devAddr', '-') if device.get('devAddr') else '-',         
                # device.get('nwkSKey', '-') if device.get('nwkSKey') else '-',         
                # device.get('appSKey', '-') if device.get('appSKey') else '-'          
            ])
    
    print(f"Devices saved to {file_path} successfully.")


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