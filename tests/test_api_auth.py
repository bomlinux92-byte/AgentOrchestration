"""Tests for API authentication middleware."""

import time
import pytest
from unittest.mock import MagicMock
from starlette.testclient import TestClient

from src.api.server import create_app
from src.api.auth_service import get_permission_service, Token, TokenState


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def ps():
    ps = get_permission_service()
    ps._revoked.clear()
    ps._token_cache.clear()
    return ps


def make_token(
    subject="user1:operator",
    scope="agent:write",
    workspace_id="ws1",
    expires_at=None,
    issued_at=None,
    revoked=False,
) -> Token:
    """Create a valid test token."""
    return Token(
        raw="test_token",
        subject=subject,
        scope=scope,
        workspace_id=workspace_id,
        expires_at=expires_at,
        issued_at=issued_at,
        revoked=revoked,
    )


class TestAuthMiddlewareTrailingSlash:
    """Regression tests for trailing slash redirect bypass on protected routes."""

    def test_trailing_slash_rejected_on_protected_route(self, client):
        """Trailing slash on protected route returns 400, not a redirect."""
        response = client.get("/api/v2/agents/")
        # Must be 400, not 307/308 redirect (which would expose the normalized path)
        assert response.status_code == 400
        assert "Trailing slash" in response.json().get("detail", "")

    def test_no_trailing_slash_passed_to_handler(self, client):
        """Non-trailing protected route passes through auth check."""
        # Without a valid token it should be 401 (auth failed), not 400 (trailing slash)
        response = client.get("/api/v2/agents")
        assert response.status_code == 401

    def test_non_protected_route_trailing_slash_accepted(self, client):
        """Trailing slash on non-protected routes does not trigger auth."""
        response = client.get("/health/")
        # /health is not protected; should pass through
        assert response.status_code == 200


class TestAuthMiddlewareTokenStates:
    """Tests prove stale, revoked, anonymous, and insufficiently scoped principals are denied."""

    def test_missing_authorization_header_rejected(self, client):
        """Anonymous request without Authorization header is denied."""
        response = client.get("/api/v2/agents")
        assert response.status_code == 401
        assert "missing" in response.json().get("detail", "").lower()

    def test_malformed_authorization_header_rejected(self, client):
        """Malformed Authorization header is denied."""
        response = client.get("/api/v2/agents", headers={"Authorization": "NotBearer token"})
        assert response.status_code == 401
        assert "malformed" in response.json().get("detail", "").lower()

    def test_empty_bearer_token_rejected(self, client):
        """Empty Bearer token is denied."""
        response = client.get("/api/v2/agents", headers={"Authorization": "Bearer "})
        assert response.status_code == 401

    def test_expired_token_rejected(self, client, ps):
        """Expired token is denied."""
        token = make_token(expires_at=time.time() - 3600)
        # Register the token in the cache
        ps._token_cache["expired_token"] = token
        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer expired_token"},
        )
        assert response.status_code == 401
        assert "expired" in response.json().get("detail", "").lower()

    def test_revoked_token_rejected(self, client, ps):
        """Revoked token is denied."""
        ps.revoke("revoked_token")
        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer revoked_token"},
        )
        assert response.status_code == 401
        assert "revoked" in response.json().get("detail", "").lower()

    def test_insufficient_scope_rejected(self, client, ps):
        """Token without required scope is denied."""
        token = make_token(scope="read:only")
        ps._token_cache["narrow_token"] = token
        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer narrow_token"},
        )
        assert response.status_code == 403
        assert "insufficient" in response.json().get("detail", "").lower()

    def test_insufficient_workspace_role_rejected(self, client, ps):
        """Token without required workspace role is denied."""
        token = make_token(subject="user1:viewer")
        ps._token_cache["viewer_token"] = token
        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer viewer_token"},
        )
        assert response.status_code == 403
        assert "insufficient" in response.json().get("detail", "").lower()

    def test_wrong_workspace_rejected(self, client, ps):
        """Token from workspace with no matching role is denied."""
        # Workspace mismatch causes role check to fail when _check_workspace_role
        # is called with the wrong workspace context.
        token = make_token(workspace_id="other_ws", subject="user2:viewer")
        ps._token_cache["other_ws_token"] = token
        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer other_ws_token"},
        )
        assert response.status_code == 403
        assert "insufficient" in response.json().get("detail", "").lower()


class TestAuthMiddlewareAuthorizedAccess:
    """Authorized users with the correct workspace role complete the same workflow successfully."""

    def test_valid_token_with_correct_scope_passes(self, client, ps):
        """Valid token with agent:write scope passes."""
        token = make_token(
            subject="user1:operator",
            scope="agent:write",
            workspace_id="ws1",
            expires_at=time.time() + 3600,
        )
        ps._token_cache["valid_token"] = token
        # With no agents registered yet, should return empty list (200)
        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer valid_token"},
        )
        assert response.status_code == 200
        assert "agents" in response.json()

    def test_valid_token_on_protected_config_route(self, client, ps):
        """Valid token on /api/v2/config is authorized."""
        token = make_token(scope="agent:write", workspace_id="ws1")
        ps._token_cache["config_token"] = token
        response = client.get(
            "/api/v2/config",
            headers={"Authorization": "Bearer config_token"},
        )
        # Should not be 401/403 — config route is protected, may 404 if no config set
        assert response.status_code != 401
        assert response.status_code != 403

    def test_valid_token_on_protected_scheduler_route(self, client, ps):
        """Valid token on /api/v2/scheduler is authorized."""
        token = make_token(scope="agent:write", workspace_id="ws1")
        ps._token_cache["scheduler_token"] = token
        response = client.get(
            "/api/v2/scheduler/jobs",
            headers={"Authorization": "Bearer scheduler_token"},
        )
        # Should not be 401/403
        assert response.status_code not in (401, 403)

    def test_valid_token_can_register_agent(self, client, ps):
        """Authorized operator can register an agent."""
        token = make_token(
            subject="user1:operator",
            scope="agent:write",
            workspace_id="ws1",
            expires_at=time.time() + 3600,
        )
        ps._token_cache["register_token"] = token
        response = client.post(
            "/api/v2/agents?name=test-agent&agent_type=test",
            headers={"Authorization": "Bearer register_token"},
        )
        assert response.status_code == 200
        assert response.json().get("status") == "registered"
        assert "agent_id" in response.json()

    def test_valid_token_can_list_agents(self, client, ps):
        """Authorized operator can list agents."""
        token = make_token(scope="agent:write", workspace_id="ws1")
        ps._token_cache["list_token"] = token
        # Register an agent first
        client.post(
            "/api/v2/agents?name=listed-agent&agent_type=test",
            headers={"Authorization": "Bearer list_token"},
        )
        response = client.get(
            "/api/v2/agents",
            headers={"Authorization": "Bearer list_token"},
        )
        assert response.status_code == 200
        assert "agents" in response.json()


class TestTokenModel:
    """Unit tests for Token and APIPermissionService."""

    def test_token_not_expired_when_no_expiry(self):
        t = Token(raw="x", expires_at=None)
        assert not t.is_expired()

    def test_token_expired_when_in_past(self):
        t = Token(raw="x", expires_at=time.time() - 1)
        assert t.is_expired()

    def test_token_not_expired_when_in_future(self):
        t = Token(raw="x", expires_at=time.time() + 3600)
        assert not t.is_expired()

    def test_has_scope_true(self):
        t = Token(raw="x", scope="agent:write agent:read")
        assert t.has_scope("agent:write")

    def test_has_scope_false(self):
        t = Token(raw="x", scope="agent:read")
        assert not t.has_scope("agent:write")

    def test_has_scope_false_when_empty(self):
        t = Token(raw="x", scope="")
        assert not t.has_scope("agent:write")

    def test_revoke_token(self, ps):
        ps.revoke("token_to_revoke")
        assert "token_to_revoke" in ps._revoked

    def test_check_permission_valid(self, ps):
        token = make_token(scope="agent:write", workspace_id="ws1")
        state = ps.check_permission(token, required_scope="agent:write")
        assert state == TokenState.VALID

    def test_check_permission_expired(self, ps):
        token = make_token(expires_at=time.time() - 100)
        state = ps.check_permission(token)
        assert state == TokenState.EXPIRED

    def test_check_permission_revoked(self, ps):
        token = Token(raw="rev", revoked=True)
        state = ps.check_permission(token)
        assert state == TokenState.REVOKED

    def test_check_permission_insufficient_scope(self, ps):
        token = make_token(scope="read")
        state = ps.check_permission(token, required_scope="write")
        assert state == TokenState.INSUFFICIENT_SCOPE

    def test_check_permission_wrong_workspace(self, ps):
        token = make_token(workspace_id="ws1")
        state = ps.check_permission(token, target_workspace_id="ws2")
        assert state == TokenState.WRONG_WORKSPACE