"""A stand-in for aiohttp's ClientSession that answers from a route table."""

# pytest idioms: fixtures as arguments, tests reach into internals and
# import optional packages late, tables of gateway answers stay on one line.
# pylint: disable=missing-function-docstring,redefined-outer-name,protected-access
# pylint: disable=import-outside-toplevel,unused-argument,line-too-long

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientResponseError


class FakeResponse:
    """Just enough of aiohttp's ClientResponse for the client."""

    def __init__(self, payload=None, status=200, body=None, cookies=None):
        self.status = status
        self._payload = payload
        self._body = body if body is not None else json.dumps(payload)
        self.cookies = {
            name: SimpleNamespace(value=value) for name, value in (cookies or {}).items()
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise ClientResponseError(MagicMock(), (), status=self.status)

    async def json(self, content_type=None):  # pylint: disable=unused-argument
        return self._payload

    async def text(self):
        return self._body

    async def read(self):
        return self._body if isinstance(self._body, bytes) else self._body.encode()


class FakeSession:
    """
    Answers requests from ``routes``: (method, url) -> FakeResponse.

    A route may also be a callable that takes the request's keyword arguments
    and returns the response, for answers that depend on the body.
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)

    def _answer(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.routes[(method, url)]
        return answer(kwargs) if callable(answer) else answer


@pytest.fixture
def session():
    return FakeSession()
