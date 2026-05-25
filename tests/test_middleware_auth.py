"""Tests for AuthMiddleware - bounty issue #4126."""

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

from src.api.middleware import AuthMiddleware, _is_internal_route, _validate_token


class TestIsInternalRoute:
    def test_openapi_json_v2(self):
        assert _is_internal_route("/api/v2/openapi.json") is True

    def test_docs_paths(self):
        assert _is_internal_route("/api/docs") is True
        assert _is_internal_route("/api/docs/swagger-ui") is True
        assert _is_internal_route("/api/redoc") is True

    def test_non_internal(self):
        assert _is_internal_route("/api/v2/agents") is False
        assert _is_internal_route("/health") is False
        assert _is_internal_route("/api/v1/agents") is False


class TestValidateToken:
    def test_valid_token(self):
        assert _validate_token("Bearer abc123") is True

    def test_empty_after_bearer(self):
        assert _validate_token("Bearer ") is False
        assert _validate_token("Bearer   ") is False

    def test_no_bearer_prefix(self):
        assert _validate_token("abc123") is False

    def test_empty_header(self):
        assert _validate_token("") is False

    def test_none_token(self):
        assert _validate_token(None) is False


class TestAuthMiddlewareIntegration:
    def create_app(self, middleware):
        async def homepage(request):
            return {"status": "ok"}

        app = Starlette(routes=[Route("/", homepage)])
        app.add_middleware(AuthMiddleware)
        return app

    def test_openapi_requires_auth(self):
        app = self.create_app(AuthMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v2/openapi.json")
        assert resp.status_code == 401

    def test_openapi_with_valid_token(self):
        # OpenAPI route passes auth check but returns 404 (no route defined in test app)
        # This verifies auth is enforced without requiring a full FastAPI app setup
        app = self.create_app(AuthMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v2/openapi.json", headers={"Authorization": "Bearer test-key"})
        # Auth passed if we get 404 (route not found) instead of 401 (unauthorized)
        assert resp.status_code == 404

    def test_docs_requires_auth(self):
        app = self.create_app(AuthMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/docs")
        assert resp.status_code == 401

    def test_redoc_requires_auth(self):
        app = self.create_app(AuthMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/redoc")
        assert resp.status_code == 401

    def test_stale_empty_bearer_rejected(self):
        app = self.create_app(AuthMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v2/openapi.json", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_anonymous_request_rejected(self):
        app = self.create_app(AuthMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v2/openapi.json")
        assert resp.status_code == 401
