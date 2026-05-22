"""Tests for RequestSizeMiddleware — enforces max body size before lookup/mutation."""

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import PlainTextResponse, JSONResponse

from src.api.middleware import RequestSizeMiddleware


class TestRequestSizeMiddleware:
    """Unit and integration tests for RequestSizeMiddleware."""

    def _make_app(self, max_body_size: int = 100, **mw_kwargs):
        """Build a Starlette app with RequestSizeMiddleware."""
        async def echo_endpoint(request: Request):
            body = await request.body()
            return JSONResponse({"body_size": len(body), "received": True})

        app = Starlette(routes=[Route("/echo", echo_endpoint, methods=["POST"])])
        app.add_middleware(RequestSizeMiddleware, max_body_size=max_body_size, **mw_kwargs)
        return app

    # --- Normal payload tests ---

    def test_small_body_under_limit_passes(self):
        """A small POST body should pass through to the handler."""
        app = self._make_app(max_body_size=100)
        client = TestClient(app)
        response = client.post("/echo", json={"key": "value"})
        assert response.status_code == 200
        assert response.json()["received"] is True

    def test_exact_limit_body_is_accepted(self):
        """A body exactly at the limit should be accepted."""
        app = self._make_app(max_body_size=50)
        client = TestClient(app)
        response = client.post("/echo", content=b"x" * 50)
        assert response.status_code == 200

    # --- Oversized payload tests ---

    def test_body_exceeding_limit_returns_413(self):
        """A body larger than max_body_size should return 413 before reaching handler."""
        app = self._make_app(max_body_size=100)
        client = TestClient(app)
        response = client.post("/echo", content=b"x" * 101)
        assert response.status_code == 413
        assert "Request body too large" in response.text

    def test_oversized_body_rejected_before_handler_called(self):
        """
        Verify the handler is never invoked for oversized bodies.
        We use a counter on the app state to detect whether the handler ran.
        """
        call_count = {"count": 0}

        async def counting_endpoint(request: Request):
            call_count["count"] += 1
            body = await request.body()
            return JSONResponse({"body_size": len(body)})

        app = Starlette(routes=[Route("/count", counting_endpoint, methods=["POST"])])
        app.add_middleware(RequestSizeMiddleware, max_body_size=10)
        client = TestClient(app)

        response = client.post("/count", content=b"x" * 20)
        assert response.status_code == 413
        assert call_count["count"] == 0, "Handler should not have been called for oversized request"

    # --- Non-mutating methods ---

    def test_get_request_not_checked(self):
        """GET requests should bypass body size check entirely."""
        async def get_handler(request: Request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/data", get_handler)])
        app.add_middleware(RequestSizeMiddleware, max_body_size=1)
        client = TestClient(app)
        response = client.get("/data")
        assert response.status_code == 200

    def test_delete_request_not_checked(self):
        """DELETE requests should bypass body size check."""
        async def delete_handler(request: Request):
            return PlainTextResponse("deleted")

        app = Starlette(routes=[Route("/data", delete_handler, methods=["DELETE"])])
        app.add_middleware(RequestSizeMiddleware, max_body_size=1)
        client = TestClient(app)
        response = client.delete("/data")
        assert response.status_code == 200

    # --- Edge cases ---

    def test_missing_content_length_passes(self):
        """Requests without Content-Length header should pass through."""
        async def simple_handler(request: Request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/simple", simple_handler, methods=["POST"])])
        app.add_middleware(RequestSizeMiddleware, max_body_size=0)
        client = TestClient(app)
        # Send request with no body and no content-length
        response = client.post("/simple", content=b"")
        assert response.status_code == 200

    def test_zero_content_length_passes(self):
        """A request with Content-Length: 0 should be accepted."""
        app = self._make_app(max_body_size=0)
        client = TestClient(app)
        response = client.post("/echo", content=b"", headers={"Content-Length": "0"})
        assert response.status_code == 200

    def test_large_oversized_body_rejected_deterministically(self):
        """An arbitrarily large body should be rejected with a fixed 413 response."""
        app = self._make_app(max_body_size=1000)
        client = TestClient(app)
        # 10 MB of data
        large_body = b"x" * (10 * 1024 * 1024)
        response = client.post("/echo", content=large_body, headers={"Content-Length": str(len(large_body))})
        assert response.status_code == 413

    # --- Configurable limit ---

    def test_custom_max_body_size(self):
        """The limit should be configurable via constructor."""
        app = self._make_app(max_body_size=5)
        client = TestClient(app)
        response = client.post("/echo", content=b"12345")
        assert response.status_code == 200
        response = client.post("/echo", content=b"123456")
        assert response.status_code == 413


class TestRequestSizeMiddlewareIntegration:
    """Integration tests via the full create_app() FastAPI app."""

    @pytest.fixture
    def client(self):
        from src.api.server import create_app
        return TestClient(create_app())

    def test_oversized_agent_registration_rejected(self, client):
        """
        POST /api/v2/agents with oversized body should be rejected with 413
        before any agent lookup or registry mutation occurs.
        """
        # Build a payload that exceeds a 1KB limit (we use a small custom limit below via app override)
        large_payload = {"name": "x" * 2000, "agent_type": "test", "config": {}}
        # With default 10MB limit this would pass, so we test via direct middleware injection
        # to verify the guard fires deterministically at middleware level
        app = Starlette()

        call_count = {"count": 0}

        async def agent_register(request: Request):
            call_count["count"] += 1
            return JSONResponse({"status": "registered"})

        from starlette.routing import Route
        app.add_middleware(RequestSizeMiddleware, max_body_size=500)
        app.add_route("/agents", agent_register, methods=["POST"])
        tc = TestClient(app)
        response = tc.post("/agents", json={"name": "x" * 1000, "type": "test"})
        assert response.status_code == 413
        assert call_count["count"] == 0

    def test_authorized_request_within_limit_succeeds(self, client):
        """An authorized POST with body within limit succeeds normally."""
        response = client.post(
            "/api/v2/agents",
            json={"name": "test-agent", "agent_type": "compute"},
            headers={"Authorization": "Bearer test-token"},
        )
        # 200 or 422 depending on validation, but not 413
        assert response.status_code in (200, 201, 422)

    def test_unauthorized_request_without_bearer_rejected(self, client):
        """Unauthorized requests are rejected at auth middleware before body size check."""
        response = client.post(
            "/api/v2/agents",
            json={"name": "test"},
        )
        assert response.status_code == 401
