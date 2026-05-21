"""Tests for SDK client — issue #1159."""
import os
import sys
sys.path.insert(0, "src")

from unittest import mock
from urllib.request import Request
from sdk.client import OrchestratorClient


def test_missing_api_key_omits_auth_header():
    """Regression test: missing API key should omit Authorization header, not send empty bearer."""
    client = OrchestratorClient(api_key=None)
    
    # Patch at the module level where it's imported
    with mock.patch("sdk.client.urlopen") as mock_urlopen:
        mock_response = mock.MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_urlopen.return_value.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_urlopen.return_value.__exit__ = mock.MagicMock(return_value=False)

        client._request("GET", "/agents")

        # Verify urlopen was called
        assert mock_urlopen.called, "urlopen should have been called"
        request_obj = mock_urlopen.call_args[0][0]
        headers = dict(request_obj.headers)
        
        # Authorization header must not be present when api_key is None/empty
        assert "Authorization" not in headers, f"Expected no Authorization header when api_key=None, got: {headers.get('Authorization')}"
        print("PASS: no Authorization header when api_key is None")


def test_explicit_api_key_sends_bearer():
    """Regression test: explicit api_key should include Authorization header."""
    client = OrchestratorClient(api_key="test-key-123")
    with mock.patch("sdk.client.urlopen") as mock_urlopen:
        mock_response = mock.MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_urlopen.return_value.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_urlopen.return_value.__exit__ = mock.MagicMock(return_value=False)

        client._request("GET", "/agents")

        request_obj = mock_urlopen.call_args[0][0]
        headers = dict(request_obj.headers)
        
        assert headers.get("Authorization") == "Bearer test-key-123", f"Expected Bearer token, got: {headers.get('Authorization')}"
        print("PASS: explicit api_key sends Authorization header")


def test_env_api_key_used():
    """Regression test: AO_API_KEY env var is used when api_key not explicitly passed."""
    with mock.patch.dict(os.environ, {"AO_API_KEY": "env-key-456"}):
        client = OrchestratorClient()
        assert client.api_key == "env-key-456", f"Expected env key, got: {client.api_key}"
        print("PASS: AO_API_KEY env var is used")


def test_empty_string_api_key_omits_auth():
    """Regression test: empty string api_key should also omit Authorization header."""
    client = OrchestratorClient(api_key="")
    with mock.patch("sdk.client.urlopen") as mock_urlopen:
        mock_response = mock.MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_urlopen.return_value.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_urlopen.return_value.__exit__ = mock.MagicMock(return_value=False)

        client._request("GET", "/agents")

        request_obj = mock_urlopen.call_args[0][0]
        headers = dict(request_obj.headers)
        
        assert "Authorization" not in headers, f"Expected no Authorization header when api_key='', got: {headers.get('Authorization')}"
        print("PASS: empty string api_key omits Authorization header")


if __name__ == "__main__":
    test_missing_api_key_omits_auth_header()
    test_explicit_api_key_sends_bearer()
    test_env_api_key_used()
    test_empty_string_api_key_omits_auth()
    print("\nAll tests passed!")
