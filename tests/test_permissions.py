"""Tests for the permission service — workspace/project role enforcement on secret metadata API."""

import pytest
import time
from src.common.permissions import (
    PermissionService,
    Principal,
    get_permission_service,
)


@pytest.fixture
def permission_service():
    """Create a fresh permission service for each test."""
    return PermissionService()


class TestPrincipal:
    """Tests for principal validation."""

    def test_anonymous_principal_denied(self, permission_service):
        """Anonymous principals must be denied."""
        principal = Principal(
            principal_id="anonymous",
            workspace_id="ws-1",
            role="viewer",
        )
        assert permission_service.validate_principal(principal) is False

    def test_empty_principal_id_denied(self, permission_service):
        """Principals with empty IDs must be denied."""
        principal = Principal(
            principal_id="",
            workspace_id="ws-1",
            role="viewer",
        )
        assert permission_service.validate_principal(principal) is False

    def test_revoked_principal_denied(self, permission_service):
        """Revoked principals must be denied."""
        principal = Principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
            is_revoked=True,
        )
        assert permission_service.validate_principal(principal) is False

    def test_expired_principal_denied(self, permission_service):
        """Expired principals must be denied."""
        principal = Principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
            issued_at=time.time() - 86400 * 2,  # issued 2 days ago
            expires_at=time.time() - 3600,       # expired 1 hour ago
        )
        assert permission_service.validate_principal(principal) is False

    def test_stale_principal_denied(self, permission_service):
        """Stale principals (older than 24h without explicit expiry) must be denied."""
        principal = Principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
            issued_at=time.time() - 86400 * 2,  # issued 2 days ago, no expiry set
        )
        assert permission_service.validate_principal(principal) is False

    def test_valid_principal_accepted(self, permission_service):
        """Valid, current, non-revoked principals must be accepted."""
        principal = Principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
            issued_at=time.time(),
        )
        assert permission_service.validate_principal(principal) is True


class TestPermissionCheck:
    """Tests for workspace-scoped permission checks."""

    def test_workspace_mismatch_denied(self, permission_service):
        """Principals cannot access resources outside their workspace."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="admin",
        )
        # Attempting to access ws-2 from ws-1 principal
        assert permission_service.check_permission(
            principal, workspace_id="ws-2", required_scope="read"
        ) is False

    def test_insufficient_scope_denied(self, permission_service):
        """Principals with viewer role cannot perform write operations."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
        )
        assert permission_service.check_permission(
            principal, workspace_id="ws-1", required_scope="write"
        ) is False

    def test_viewer_can_read(self, permission_service):
        """Viewers can read within their workspace."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
        )
        assert permission_service.check_permission(
            principal, workspace_id="ws-1", required_scope="read"
        ) is True

    def test_editor_can_write(self, permission_service):
        """Editors can write within their workspace."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="editor",
        )
        assert permission_service.check_permission(
            principal, workspace_id="ws-1", required_scope="write"
        ) is True

    def test_admin_can_delete(self, permission_service):
        """Admins can delete within their workspace."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="admin",
        )
        assert permission_service.check_permission(
            principal, workspace_id="ws-1", required_scope="delete"
        ) is True

    def test_invalid_role_denied(self, permission_service):
        """Unknown roles should have minimal permissions (deny write)."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="unknown",
        )
        assert permission_service.check_permission(
            principal, workspace_id="ws-1", required_scope="read"
        ) is False
        assert permission_service.check_permission(
            principal, workspace_id="ws-1", required_scope="write"
        ) is False


class TestEnvVarReadEnforcement:
    """Tests specifically for env var read (secret metadata API) enforcement."""

    def test_env_var_read_requires_authentication(self, permission_service):
        """Env var reads require a valid, authenticated principal."""
        # Anonymous principal
        anon = Principal(principal_id="anonymous", workspace_id="ws-1", role="viewer")
        assert permission_service.check_env_var_read(anon, workspace_id="ws-1") is False

    def test_env_var_read_requires_workspace_match(self, permission_service):
        """Env var reads must be scoped to the caller's workspace."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
        )
        # Trying to read env vars in a different workspace
        assert permission_service.check_env_var_read(principal, workspace_id="ws-2") is False

    def test_env_var_read_allowed_for_viewer_in_same_workspace(self, permission_service):
        """Viewers can read env vars in their own workspace."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
        )
        assert permission_service.check_env_var_read(principal, workspace_id="ws-1") is True

    def test_stale_token_denied_on_env_var_read(self, permission_service):
        """Stale tokens are denied on env var reads."""
        principal = Principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
            issued_at=time.time() - 86400 * 3,  # stale
        )
        assert permission_service.check_env_var_read(principal, workspace_id="ws-1") is False

    def test_revoked_token_denied_on_env_var_read(self, permission_service):
        """Revoked tokens are denied on env var reads."""
        principal = Principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="viewer",
            is_revoked=True,
        )
        assert permission_service.check_env_var_read(principal, workspace_id="ws-1") is False


class TestRevocation:
    """Tests for token/principal revocation."""

    def test_revoke_principal_denies_all(self, permission_service):
        """Revoking a principal immediately denies all their requests."""
        principal = permission_service.get_or_create_principal(
            principal_id="user-1",
            workspace_id="ws-1",
            role="admin",
        )
        permission_service.revoke_principal("user-1")
        assert permission_service.validate_principal(principal) is False
        assert permission_service.check_env_var_read(principal, workspace_id="ws-1") is False


class TestGlobalSingleton:
    """Tests for the global singleton."""

    def test_get_permission_service_returns_singleton(self):
        """get_permission_service should return the same instance."""
        svc1 = get_permission_service()
        svc2 = get_permission_service()
        assert svc1 is svc2
