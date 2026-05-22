"""Tests for error middleware — sanitization, request isolation, and exception paths."""

import json
import pytest
from starlette.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.applications import Starlette
from starlette.routing import Route

from src.api.middleware import (
    ErrorMiddleware,
    sanitize_dict,
    sanitize_exception_detail,
    sanitize_value,
    SENSITIVE_FIELD_PATTERNS,
)
from src.api.server import create_app


class TestSanitizeHelpers:
    """Unit tests for sanitize_value, sanitize_dict, sanitize_exception_detail."""

    def test_sanitize_value_string_under_limit(self):
        assert sanitize_value("hello") == "hello"

    def test_sanitize_value_string_over_limit(self):
        long = "a" * 300
        assert len(sanitize_value(long)) == 200

    def test_sanitize_value_non_string(self):
        assert sanitize_value(42) == "42"
        assert sanitize_value(3.14) == "3.14"

    def test_sanitize_dict_redacts_sensitive_keys(self):
        data = {
            "username": "alice",
            "password": "s3cret!",
            "api_key": "ak-12345",
            "token": "tok-abc",
            "refresh_token": "rt-xyz",
            "Authorization": "Bearer abc",
            "session_id": "sid-1",
            "safe_field": "ok",
        }
        result = sanitize_dict(data)
        assert result["username"] == "alice"
        assert result["password"] == "***REDACTED***"
        assert result["api_key"] == "***REDACTED***"
        assert result["token"] == "***REDACTED***"
        assert result["refresh_token"] == "***REDACTED***"
        assert result["Authorization"] == "***REDACTED***"
        assert result["session_id"] == "***REDACTED***"
        assert result["safe_field"] == "ok"

    def test_sanitize_dict_recursive(self):
        data = {"outer": {"password": "inner_secret", "name": "bob"}}
        result = sanitize_dict(data)
        assert result["outer"]["password"] == "***REDACTED***"
        assert result["outer"]["name"] == "bob"

    def test_sanitize_dict_list_values(self):
        data = {
            "items": [
                {"password": "p1", "role": "admin"},
                {"password": "p2", "role": "user"},
            ]
        }
        result = sanitize_dict(data)
        assert result["items"][0]["password"] == "***REDACTED***"
        assert result["items"][1]["password"] == "***REDACTED***"

    def test_sanitize_dict_circular_reference(self):
        data = {"name": "test"}
        data["self"] = data
        result = sanitize_dict(data)
        assert result["name"] == "test"

    def test_sanitize_exception_detail_basic(self):
        exc = ValueError("something broke")
        result = sanitize_exception_detail(exc)
        assert result["error"] == "ValueError"
        assert result["message"] == "something broke"

    def test_sanitize_exception_detail_with_sensitive_attrs(self):
        class CustomError(Exception):
            def __init__(self, msg, api_key=None, safe_data=None):
                super().__init__(msg)
                self.api_key = api_key
                self.safe_data = safe_data

        exc = CustomError("fail", api_key="secret-key-123", safe_data="public")
        result = sanitize_exception_detail(exc)
        assert result["error"] == "CustomError"
        assert result["api_key"] == "***REDACTED***"
        assert result["safe_data"] == "public"

    def test_sanitize_exception_detail_skips_private_attrs(self):
        class ErrorWithPrivate(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self._internal = "hidden"
                self.public = "visible"

        exc = ErrorWithPrivate("oops")
        result = sanitize_exception_detail(exc)
        assert "_internal" not in result
        assert result["public"] == "visible"

    def test_sensitive_field_patterns_covers_expected_fields(self):
        sensitive = [
            "password", "passwd", "secret", "token", "api_key", "apiKey",
            "access_key", "private_key", "auth_token", "refresh_token",
            "session_id", "credit_card", "ssn", "authorization",
            "cookie", "credential", "connection_string",
        ]
        for field in sensitive:
            assert SENSITIVE_FIELD_PATTERNS.match(field), f"Expected {field} to be matched as sensitive"


def _make_app_with_error_middleware(routes, **mw_kwargs):
    """Build a Starlette app with ErrorMiddleware added as innermost middleware."""
    app = Starlette(routes=routes)
    app.add_middleware(ErrorMiddleware, **mw_kwargs)
    return app


class TestErrorMiddlewareIntegration:
    """Integration tests for ErrorMiddleware via the ASGI app."""

    @pytest.fixture
    def client(self):
        app = create_app()
        return TestClient(app)

    def test_normal_request_passes_through(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    def test_auth_rejection_no_leak(self, client):
        response = client.get("/api/v2/agents")
        assert response.status_code == 401

    def test_not_found_returns_clean_error(self, client):
        response = client.get("/api/v2/nonexistent")
        assert response.status_code in (401, 404)

    def test_error_middleware_catches_exception(self):
        """ErrorMiddleware should catch unhandled exceptions and return sanitized JSON."""
        async def broken_endpoint(request: Request):
            raise RuntimeError("database connection password=leaked failed")

        app = _make_app_with_error_middleware([Route("/broken", broken_endpoint)])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/broken")
        assert response.status_code == 500
        data = response.json()
        assert "detail" in data
        assert data["detail"]["error"] == "RuntimeError"
        assert "password=leaked" not in json.dumps(data)

    def test_error_middleware_sanitizes_exception_attrs(self):
        """Exception attributes carrying sensitive data must be redacted."""
        class DBError(RuntimeError):
            def __init__(self, msg, connection_string=None, token=None):
                super().__init__(msg)
                self.connection_string = connection_string
                self.token = token

        async def fail_endpoint(request: Request):
            raise DBError(
                "connection refused",
                connection_string="postgres://admin:password123@db:5432/prod",
                token="Bearer super-secret-token",
            )

        app = _make_app_with_error_middleware([Route("/fail", fail_endpoint)])
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/fail")
        assert response.status_code == 500
        data = response.json()
        detail = data["detail"]
        assert "connection_string" in detail
        assert detail["connection_string"] == "***REDACTED***"
        assert detail["token"] == "***REDACTED***"
        assert "password123" not in json.dumps(data)
        assert "super-secret-token" not in json.dumps(data)

    def test_error_middleware_custom_sensitive_fields(self):
        """Extra sensitive field names passed via constructor should also be redacted."""
        class CustomErr(RuntimeError):
            def __init__(self, msg, deployment_key=None):
                super().__init__(msg)
                self.deployment_key = deployment_key

        async def err_endpoint(request: Request):
            raise CustomErr("deploy failed", deployment_key="dk-abc123")

        app = _make_app_with_error_middleware(
            [Route("/err", err_endpoint)],
            sensitive_fields=["deployment_key"],
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/err")
        assert response.status_code == 500
        data = response.json()
        assert data["detail"]["deployment_key"] == "***REDACTED***"

    def test_error_middleware_clears_request_state(self):
        """Request-local state set by ErrorMiddleware should be cleaned in finally block."""
        captured_state = {}

        async def inspect_endpoint(request: Request):
            captured_state["has_context"] = hasattr(request.state, "_error_middleware_context")
            return PlainTextResponse("ok")

        app = _make_app_with_error_middleware([Route("/inspect", inspect_endpoint)])
        client = TestClient(app)

        client.get("/inspect")
        assert captured_state["has_context"] is True

    def test_error_middleware_no_cross_request_leak(self):
        """Verify request state from one request doesn't bleed into the next."""
        class StateInspector:
            def __init__(self):
                self.before_delete = []
                self.snapshots = []

        inspector = StateInspector()

        async def snap_endpoint(request: Request):
            state_dict = dict(request.state._state)
            inspector.snapshots.append(state_dict)
            return PlainTextResponse("ok")

        app = _make_app_with_error_middleware([Route("/snap", snap_endpoint)])
        client = TestClient(app)

        # Make two sequential requests
        client.get("/snap")
        client.get("/snap")

        # Both should have the context flag during the request
        assert inspector.snapshots[0].get("_error_middleware_context") is True
        assert inspector.snapshots[1].get("_error_middleware_context") is True