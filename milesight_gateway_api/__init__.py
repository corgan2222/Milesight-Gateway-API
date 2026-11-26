# milesight_gateway_api/__init__.py
from .exceptions import (
    MilesightGatewayError,
    UnexpectedResponseError,
    WebConfigError,
    WebLoginRefusedError,
    WebSessionError,
)
from .firmware import (
    FirmwareRelease,
    download_firmware,
    firmware_key,
    get_firmware_releases,
    get_latest_firmware,
    next_firmware,
    parse_firmware_page,
    parse_firmware_releases,
)
from .milesight_gateway_client import MilesightGatewayClient

__all__ = [
    "FirmwareRelease",
    "MilesightGatewayClient",
    "MilesightGatewayError",
    "UnexpectedResponseError",
    "WebConfigError",
    "WebLoginRefusedError",
    "WebSessionError",
    "download_firmware",
    "firmware_key",
    "get_firmware_releases",
    "get_latest_firmware",
    "next_firmware",
    "parse_firmware_page",
    "parse_firmware_releases",
]
