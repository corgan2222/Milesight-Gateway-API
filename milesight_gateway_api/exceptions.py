"""Errors the client raises besides aiohttp's own HTTP and network errors."""


class MilesightGatewayError(Exception):
    """Base class for errors that are about the gateway's answer, not the network."""


class UnexpectedResponseError(MilesightGatewayError):
    """The gateway answered, but not in the shape its API documents."""


class WebSessionError(MilesightGatewayError):
    """The web interface dropped the session again right after a new login."""


class WebLoginRefusedError(MilesightGatewayError):
    """
    The web interface refused the login.

    The gateway locks an account after a few failed web logins, which also
    locks its owner out of the web interface. After one refusal the client
    therefore raises this without trying again, until it is created anew.
    """
