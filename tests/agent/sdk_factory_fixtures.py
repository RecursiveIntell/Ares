"""Constructor-only SDK doubles with an owned real HTTPX boundary.

These fixtures preserve argument-capture tests without disabling production
binding checks. They do not qualify SDK serialization or provider transport;
separate real-SDK/loopback tests own those assertions.
"""
from contextlib import contextmanager
import httpx


def _no_network(request):
    raise AssertionError('constructor-only fixture attempted HTTP transport')


class _ConstructorClient:
    def __init__(self, kwargs):
        self._client=httpx.Client(transport=httpx.MockTransport(_no_network),trust_env=False)
        self.api_key=kwargs.get('api_key')
        self.auth_token=kwargs.get('auth_token')
        self.base_url=kwargs.get('base_url') or 'https://api.anthropic.com'

    def request(self,*args,**kwargs):
        raise AssertionError('constructor-only fixture attempted an SDK request')

    def copy(self,*args,**kwargs):
        raise AssertionError('constructor-only fixture attempted an SDK copy')

    def close(self):
        self._client.close()


@contextmanager
def constructor_httpx_backend(mock_sdk):
    clients=[]
    previous=mock_sdk.Anthropic.side_effect
    def construct(*args,**kwargs):
        assert not args,'constructor fixture expects keyword arguments'
        client=_ConstructorClient(kwargs)
        clients.append(client)
        mock_sdk.Anthropic.return_value=client
        return client
    mock_sdk.Anthropic.side_effect=construct
    try:yield mock_sdk
    finally:
        mock_sdk.Anthropic.side_effect=previous
        for client in clients:client.close()


@contextmanager
def mock_client_httpx_backend(client):
    """Give an explicitly mocked SDK client the required HTTPX shape."""
    with httpx.Client(transport=httpx.MockTransport(_no_network),trust_env=False) as http:
        client._client=http
        yield client
