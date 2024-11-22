import asyncio
import logging
import json
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
            
            # Fetch gateway fleet
            organization_id = "1"
            gateway_fleet, total_gateways = await client.get_gateway_fleet(session, organization_id)
            print(f"Total Gateways: {total_gateways}")
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