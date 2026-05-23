"""Tests for API middleware."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from starlette.requests import Request
from starlette.responses import Response

from src.api.middleware import AuthMiddleware, RateLimitMiddleware, LoggingMiddleware


class TestAuthMiddleware:
    """Tests for AuthMiddleware bearer scheme validation."""

    def _build_request(self, path: str, auth_header: str = None) -> Request:
        """Helper to build a mock request."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
        }
        if auth_header:
            scope["headers"] = [(b"authorization", auth_header.encode())]
        request = Request(scope)
        return request

    @pytest.mark.asyncio
    async def test_valid_bearer_scheme_uppercase(self):
        """Test that 'Bearer' (uppercase) is accepted."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/agents", "Bearer valid-token")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_valid_bearer_scheme_lowercase(self):
        """Test that 'bearer' (lowercase) is accepted via case-insensitive comparison."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/agents", "bearer valid-token")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_scheme_rejected(self):
        """Test that non-Bearer schemes (e.g., 'Basic') are rejected."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/agents", "Basic dXNlcjpwYXNz")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        assert response is not None
        assert response.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_bearer_without_token_rejected(self):
        """Test that 'Bearer' without credentials is rejected."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/agents", "Bearer")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        assert response is not None
        assert response.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_auth_header_rejected(self):
        """Test that empty Authorization header is rejected."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/agents", "")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        assert response is not None
        assert response.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_auth_header_rejected(self):
        """Test that missing Authorization header is rejected."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/agents")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        assert response is not None
        assert response.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_auth_token_endpoint_allowed(self):
        """Test that /api/v2/auth/token endpoint skips auth."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/auth/token")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_api_v2_path_allowed(self):
        """Test that non-/api/v2 paths skip auth middleware."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v1/agents")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_mixed_case_bearer_accepted(self):
        """Test that mixed case like 'BEARER' or 'BeArEr' is accepted."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/agents", "BEARER valid-token")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_bearer_with_extra_whitespace_rejected(self):
        """Test that 'Bearer  ' (empty token after space) is rejected."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._build_request("/api/v2/agents", "Bearer  ")
        call_next = AsyncMock(return_value=Response(status_code=200))
        response = await middleware.dispatch(request, call_next)
        assert response is not None
        assert response.status_code == 401
        call_next.assert_not_called()


class TestRateLimitMiddleware:
    """Tests for RateLimitMiddleware."""

    @pytest.mark.asyncio
    async def test_rate_limit_allows_within_limit(self):
        """Test that requests within rate limit are allowed."""
        middleware = RateLimitMiddleware(app=MagicMock(), max_requests=5, window=60)
        call_next = AsyncMock(return_value=Response(status_code=200))
        
        # Simulate 3 requests - should be allowed
        for _ in range(3):
            request = MagicMock()
            request.client.host = "192.168.1.1"
            await middleware.dispatch(request, call_next)
        
        # All should have passed
        assert call_next.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_when_exceeded(self):
        """Test that requests exceeding rate limit are blocked."""
        middleware = RateLimitMiddleware(app=MagicMock(), max_requests=2, window=60)
        client_ip = "192.168.1.2"
        call_next = AsyncMock(return_value=Response(status_code=200))
        
        # First two requests should pass
        for _ in range(2):
            request = MagicMock()
            request.client.host = client_ip
            await middleware.dispatch(request, call_next)
        
        # Third request should be blocked
        request = MagicMock()
        request.client.host = client_ip
        response = await middleware.dispatch(request, call_next)
        assert response is not None
        assert response.status_code == 429


class TestLoggingMiddleware:
    """Tests for LoggingMiddleware."""

    @pytest.mark.asyncio
    async def test_logging_middleware_passes_through(self):
        """Test that logging middleware passes request to call_next."""
        middleware = LoggingMiddleware(app=MagicMock())
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/test"
        response = Response(status_code=200)
        
        call_next = AsyncMock(return_value=response)
        result = await middleware.dispatch(request, call_next)
        assert result.status_code == 200
        call_next.assert_called_once()