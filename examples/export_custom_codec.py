import asyncio
import logging
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

            # Get all payload codecs of type 'custom'
            get_codec_type = "custom"
            custom_payload_codecs, codecs_total_count = await client.get_payload_codecs(session, codec_type=get_codec_type)                        
            print(f"Total {get_codec_type} Codecs: {codecs_total_count}")
            save_payload_codecs(custom_payload_codecs)
            

        except Exception as e:
            print(f"Error: {e}")


def save_payload_codecs(data, base_dir='export/payload_codecs'):
    """
    Saves encoder and decoder scripts from the JSON data to files in the specified directory structure.

    Args:
    data (dict or list): The JSON data containing the encoder and decoder scripts.
    base_dir (str): The base directory where the folders and files will be created (default is 'payload_codecs').
    """
    # Ensure the base directory exists
    os.makedirs(base_dir, exist_ok=True)

    # Check if the input is a dictionary with a 'result' field or a list directly
    if isinstance(data, dict):
        result = data.get("result", [])
    elif isinstance(data, list):
        result = data  # If data is already a list, use it directly
    else:
        raise TypeError("Expected input to be a dictionary with 'result' or a list.")

    # Iterate over the result list
    for item in result:
        folder_name = os.path.join(base_dir, item["name"])
        
        # Create a folder for each entry
        os.makedirs(folder_name, exist_ok=True)
        
        # Define the file paths
        encoder_script_path = os.path.join(folder_name, f'{item["name"]}_encoder.js')
        decoder_script_path = os.path.join(folder_name, f'{item["name"]}_decoder.js')

        # Save encoderScript to a file with UTF-8 encoding
        with open(encoder_script_path, 'w', encoding='utf-8') as encoder_file:
            encoder_file.write(item.get("encoderScript", "").replace('\\n', '\n'))
        
        # Save decoderScript to a file with UTF-8 encoding
        with open(decoder_script_path, 'w', encoding='utf-8') as decoder_file:
            decoder_file.write(item.get("decoderScript", "").replace('\\n', '\n'))
    
    print(f"Scripts saved successfully in '{base_dir}'.")


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