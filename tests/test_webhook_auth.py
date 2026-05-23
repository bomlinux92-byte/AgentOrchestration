"""Tests for webhook auth guard and disabled principal blocking."""

import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch

from starlette.requests import Request
from starlette.responses import Response

from src.common.errors import DisabledPrincipalError, InvalidPrincipalError
from src.common.webhook_auth import (
    PrincipalState,
    PrincipalStore,
    WebhookAuthGuard,
    get_principal_store,
    reset_principal_store,
    reset_webhook_auth_guard,
)
from src.api.middleware import AuthMiddleware


class TestPrincipalState:
    """Tests for PrincipalState validation."""

    def test_active_principal_is_valid(self):
        """An enabled, non-revoked, non-expired principal should be active."""
        state = PrincipalState(
            principal_id="user-123",
            is_enabled=True,
            is_revoked=False,
            expires_at=time.time() + 3600,  # 1 hour from now
        )
        assert state.is_active() is True
        state.validate()  # Should not raise

    def test_disabled_principal_raises(self):
        """A disabled principal should raise DisabledPrincipalError on validate."""
        state = PrincipalState(
            principal_id="user-123",
            is_enabled=False,
        )
        assert state.is_active() is False
        with pytest.raises(DisabledPrincipalError) as exc_info:
            state.validate()
        assert exc_info.value.principal_id == "user-123"

    def test_revoked_principal_raises(self):
        """A revoked principal should raise InvalidPrincipalError on validate."""
        state = PrincipalState(
            principal_id="user-123",
            is_enabled=True,
            is_revoked=True,
        )
        assert state.is_active() is False
        with pytest.raises(InvalidPrincipalError) as exc_info:
            state.validate()
        assert exc_info.value.principal_id == "user-123"

    def test_expired_principal_raises(self):
        """An expired principal should raise InvalidPrincipalError on validate."""
        state = PrincipalState(
            principal_id="user-123",
            is_enabled=True,
            expires_at=time.time() - 1,  # 1 second in the past
        )
        assert state.is_active() is False
        with pytest.raises(InvalidPrincipalError) as exc_info:
            state.validate()
        assert exc_info.value.principal_id == "user-123"

    def test_never_expires_principal_is_valid(self):
        """A principal with expires_at=None should never expire."""
        state = PrincipalState(
            principal_id="user-123",
            is_enabled=True,
            expires_at=None,
        )
        assert state.is_active() is True


class TestPrincipalStore:
    """Tests for PrincipalStore."""

    def setup_method(self):
        reset_principal_store()

    def teardown_method(self):
        reset_principal_store()

    def test_register_and_get(self):
        """Test registering a principal and retrieving it."""
        store = get_principal_store()
        state = store.register("user-123", is_enabled=True)
        assert state.principal_id == "user-123"
        assert state.is_enabled is True

        retrieved = store.get("user-123")
        assert retrieved is state

    def test_get_nonexistent_returns_none(self):
        """Getting a nonexistent principal returns None."""
        store = get_principal_store()
        assert store.get("nonexistent") is None

    def test_set_enabled(self):
        """Test enabling and disabling a principal."""
        store = get_principal_store()
        store.register("user-123")

        assert store.set_enabled("user-123", False) is True
        assert store.get("user-123").is_enabled is False

        assert store.set_enabled("user-123", True) is True
        assert store.get("user-123").is_enabled is True

    def test_set_enabled_nonexistent_returns_false(self):
        """Setting enabled on nonexistent principal returns False."""
        store = get_principal_store()
        assert store.set_enabled("nonexistent", False) is False

    def test_revoke(self):
        """Test revoking a principal."""
        store = get_principal_store()
        store.register("user-123")

        assert store.revoke("user-123") is True
        assert store.get("user-123").is_revoked is True

    def test_revoke_nonexistent_returns_false(self):
        """Revoking nonexistent principal returns False."""
        store = get_principal_store()
        assert store.revoke("nonexistent") is False


class TestWebhookAuthGuard:
    """Tests for WebhookAuthGuard."""

    def setup_method(self):
        reset_webhook_auth_guard()

    def teardown_method(self):
        reset_webhook_auth_guard()

    def _build_guard_with_principal(self, principal_id: str, **kwargs) -> WebhookAuthGuard:
        """Helper to create a guard with a pre-registered principal."""
        guard = WebhookAuthGuard()
        guard.store.register(principal_id, **kwargs)
        return guard

    def test_check_principal_active_succeeds(self):
        """An active principal should pass check_principal."""
        guard = self._build_guard_with_principal(
            "user-123",
            is_enabled=True,
            expires_at=time.time() + 3600,
        )
        state = guard.check_principal("user-123")
        assert state.principal_id == "user-123"

    def test_check_principal_disabled_raises(self):
        """A disabled principal should raise DisabledPrincipalError."""
        guard = self._build_guard_with_principal(
            "user-123",
            is_enabled=False,
        )
        with pytest.raises(DisabledPrincipalError) as exc_info:
            guard.check_principal("user-123")
        assert exc_info.value.principal_id == "user-123"

    def test_check_principal_revoked_raises(self):
        """A revoked principal should raise InvalidPrincipalError."""
        guard = WebhookAuthGuard()
        guard.store.register("user-123", is_enabled=True)
        guard.store.revoke("user-123")

        with pytest.raises(InvalidPrincipalError) as exc_info:
            guard.check_principal("user-123")
        assert exc_info.value.principal_id == "user-123"

    def test_check_principal_expired_raises(self):
        """An expired principal should raise InvalidPrincipalError."""
        guard = self._build_guard_with_principal(
            "user-123",
            is_enabled=True,
            expires_at=time.time() - 1,
        )
        with pytest.raises(InvalidPrincipalError) as exc_info:
            guard.check_principal("user-123")
        assert exc_info.value.principal_id == "user-123"

    def test_check_principal_not_found_raises(self):
        """A nonexistent principal should raise InvalidPrincipalError."""
        guard = WebhookAuthGuard()
        with pytest.raises(InvalidPrincipalError) as exc_info:
            guard.check_principal("nonexistent")
        assert "not found" in exc_info.value.reason

    def test_can_manage_webhooks_with_scope(self):
        """A principal with webhook scope can manage webhooks."""
        guard = self._build_guard_with_principal(
            "user-123",
            is_enabled=True,
            scopes={"webhook:read", "webhook:write"},
        )
        assert guard.can_manage_webhooks("user-123") is True

    def test_can_manage_webhooks_without_scope(self):
        """A principal without webhook scope cannot manage webhooks."""
        guard = self._build_guard_with_principal(
            "user-123",
            is_enabled=True,
            scopes={"agent:read"},
        )
        assert guard.can_manage_webhooks("user-123") is False

    def test_can_manage_webhooks_disabled_returns_false(self):
        """A disabled principal cannot manage webhooks."""
        guard = self._build_guard_with_principal(
            "user-123",
            is_enabled=False,
            scopes={"webhook:manage"},
        )
        assert guard.can_manage_webhooks("user-123") is False

    def test_can_manage_webhooks_revoked_returns_false(self):
        """A revoked principal cannot manage webhooks."""
        guard = WebhookAuthGuard()
        guard.store.register("user-123", is_enabled=True, scopes={"webhook:manage"})
        guard.store.revoke("user-123")
        assert guard.can_manage_webhooks("user-123") is False

    def test_invalidate_principal(self):
        """Test invalidating (disabling + revoking) a principal."""
        guard = self._build_guard_with_principal(
            "user-123",
            is_enabled=True,
        )
        guard.invalidate_principal("user-123")

        state = guard.store.get("user-123")
        assert state.is_enabled is False
        assert state.is_revoked is True


class TestAuthMiddlewareWebhookBlocking:
    """Tests for AuthMiddleware blocking disabled principals from webhook management."""

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
        return Request(scope)

    @pytest.mark.asyncio
    async def test_webhook_endpoint_disabled_principal_blocked(self):
        """Test that a disabled principal is blocked from webhook endpoints."""
        guard = WebhookAuthGuard()
        guard.store.register("disabled-user", is_enabled=False, scopes={"webhook:manage"})

        middleware = AuthMiddleware(app=MagicMock(), webhook_guard=guard)
        request = self._build_request("/api/v2/webhooks", "Bearer disabled-user")
        call_next = AsyncMock(return_value=Response(status_code=200))

        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 403
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_endpoint_revoked_principal_blocked(self):
        """Test that a revoked principal is blocked from webhook endpoints."""
        guard = WebhookAuthGuard()
        guard.store.register("revoked-user", is_enabled=True, scopes={"webhook:manage"})
        guard.store.revoke("revoked-user")

        middleware = AuthMiddleware(app=MagicMock(), webhook_guard=guard)
        request = self._build_request("/api/v2/webhooks", "Bearer revoked-user")
        call_next = AsyncMock(return_value=Response(status_code=200))

        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_endpoint_expired_principal_blocked(self):
        """Test that an expired principal is blocked from webhook endpoints."""
        guard = WebhookAuthGuard()
        guard.store.register(
            "expired-user",
            is_enabled=True,
            expires_at=time.time() - 1,
            scopes={"webhook:manage"},
        )

        middleware = AuthMiddleware(app=MagicMock(), webhook_guard=guard)
        request = self._build_request("/api/v2/webhooks", "Bearer expired-user")
        call_next = AsyncMock(return_value=Response(status_code=200))

        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_endpoint_invalid_principal_blocked(self):
        """Test that an invalid (not found) principal is blocked."""
        guard = WebhookAuthGuard()

        middleware = AuthMiddleware(app=MagicMock(), webhook_guard=guard)
        request = self._build_request("/api/v2/webhooks", "Bearer unknown-user")
        call_next = AsyncMock(return_value=Response(status_code=200))

        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_endpoint_valid_principal_allowed(self):
        """Test that a valid principal can access webhook endpoints."""
        guard = WebhookAuthGuard()
        guard.store.register(
            "active-user",
            is_enabled=True,
            scopes={"webhook:read"},
        )

        middleware = AuthMiddleware(app=MagicMock(), webhook_guard=guard)
        request = self._build_request("/api/v2/webhooks", "Bearer active-user")
        call_next = AsyncMock(return_value=Response(status_code=200))

        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 200
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_integration_endpoint_disabled_principal_blocked(self):
        """Test that a disabled principal is blocked from integration endpoints."""
        guard = WebhookAuthGuard()
        guard.store.register("disabled-integration", is_enabled=False, scopes={"webhook:manage"})

        middleware = AuthMiddleware(app=MagicMock(), webhook_guard=guard)
        request = self._build_request("/api/v2/integrations", "Bearer disabled-integration")
        call_next = AsyncMock(return_value=Response(status_code=200))

        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 403
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_webhook_endpoint_skips_principal_check(self):
        """Test that non-webhook endpoints skip the principal check."""
        guard = WebhookAuthGuard()
        guard.store.register("any-user", is_enabled=False, scopes={"webhook:manage"})

        middleware = AuthMiddleware(app=MagicMock(), webhook_guard=guard)
        request = self._build_request("/api/v2/agents", "Bearer any-user")
        call_next = AsyncMock(return_value=Response(status_code=200))

        response = await middleware.dispatch(request, call_next)

        # Should pass because it's not a webhook endpoint
        assert response.status_code == 200
        call_next.assert_called_once()