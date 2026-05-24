"""Tests for CORS credential enforcement middleware."""

import logging
import pytest
from unittest.mock import patch
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from src.api.middleware import CORSCredentialMiddleware
from starlette.middleware import Middleware


def _make_app(allowed_origins):
    """Create a minimal Starlette app with CORSCredentialMiddleware."""
    async def health(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/health", health)],
        middleware=[
            Middleware(CORSCredentialMiddleware, allowed_origins=allowed_origins),
        ],
    )
    return app


@pytest.fixture
def client_allowed():
    return TestClient(_make_app(["https://trusted.example.com", "https://app.example.com"]))


@pytest.fixture
def client_wildcard():
    return TestClient(_make_app(["*"]))


class TestCORSCredentialAllowedRequests:
    """Credentialed requests from allowlisted origins pass through."""

    def test_auth_header_allowed_origin(self, client_allowed):
        resp = client_allowed.get(
            "/health",
            headers={"Origin": "https://trusted.example.com", "Authorization": "Bearer tok"},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "https://trusted.example.com"

    def test_cookie_allowed_origin(self, client_allowed):
        resp = client_allowed.get(
            "/health",
            headers={"Origin": "https://app.example.com", "Cookie": "session=abc"},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "https://app.example.com"

    def test_vary_header_set(self, client_allowed):
        resp = client_allowed.get(
            "/health",
            headers={"Origin": "https://trusted.example.com", "Authorization": "Bearer tok"},
        )
        assert "Origin" in resp.headers.get("vary", "")


class TestCORSCredentialRejectedRequests:
    """Credentialed requests from non-allowlisted origins are rejected."""

    def test_auth_header_rejected_origin(self, client_allowed):
        resp = client_allowed.get(
            "/health",
            headers={"Origin": "https://evil.example.com", "Authorization": "Bearer tok"},
        )
        assert resp.status_code == 403
        assert "CORS policy" in resp.json()["detail"]

    def test_cookie_rejected_origin(self, client_allowed):
        resp = client_allowed.get(
            "/health",
            headers={"Origin": "https://evil.example.com", "Cookie": "session=abc"},
        )
        assert resp.status_code == 403

    def test_wildcard_rejects_all_credentialed(self, client_wildcard):
        """Wildcard is never valid for credentialed requests."""
        resp = client_wildcard.get(
            "/health",
            headers={"Origin": "https://any.example.com", "Authorization": "Bearer tok"},
        )
        assert resp.status_code == 403


class TestCORSNonCredentialedRequests:
    """Non-credentialed or non-browser requests pass through."""

    def test_origin_without_credentials_passes(self, client_allowed):
        resp = client_allowed.get(
            "/health",
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.status_code == 200

    def test_no_origin_with_credentials_passes(self, client_allowed):
        """No Origin = non-browser client; not subject to CORS enforcement."""
        resp = client_allowed.get(
            "/health",
            headers={"Authorization": "Bearer tok"},
        )
        assert resp.status_code == 200

    def test_wildcard_non_credentialed_passes(self, client_wildcard):
        resp = client_wildcard.get(
            "/health",
            headers={"Origin": "https://any.example.com"},
        )
        assert resp.status_code == 200


class TestCORSStateIsolation:
    """Verify no state leaks between requests."""

    def test_allowed_then_rejected_then_allowed(self, client_allowed):
        r1 = client_allowed.get(
            "/health",
            headers={"Origin": "https://trusted.example.com", "Authorization": "Bearer t1"},
        )
        assert r1.status_code == 200

        r2 = client_allowed.get(
            "/health",
            headers={"Origin": "https://evil.example.com", "Authorization": "Bearer t2"},
        )
        assert r2.status_code == 403

        r3 = client_allowed.get(
            "/health",
            headers={"Origin": "https://app.example.com", "Authorization": "Bearer t3"},
        )
        assert r3.status_code == 200


class TestCORSLoggingSafety:
    """Ensure logs do not expose sensitive header values."""

    def test_rejection_log_omits_credentials(self, client_allowed, caplog):
        with caplog.at_level(logging.WARNING):
            client_allowed.get(
                "/health",
                headers={
                    "Origin": "https://evil.example.com",
                    "Authorization": "Bearer supersecrettoken123",
                    "Cookie": "session=secretvalue",
                },
            )
        for record in caplog.records:
            assert "supersecrettoken123" not in record.message
            assert "secretvalue" not in record.message
