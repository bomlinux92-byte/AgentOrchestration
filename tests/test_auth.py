"""Tests for authentication service and API key revalidation.

These tests verify that revoked/disabled/expired/anonymous/insufficient-scope
API keys are properly denied on every request, especially during long-polling.

Issue #625: Revalidate revoked API keys on long polling
"""

import pytest
import time
from src.common.auth import (
    AuthService,
    KeyStatus,
    get_auth_service,
    set_auth_service,
)


class TestAuthService:
    """Test the AuthService API key validation."""

    def setup_method(self):
        """Set up a fresh AuthService for each test."""
        self.auth = AuthService()
        # Register a valid test key
        self.valid_key = self.auth.register_key(
            key_id="test-key-001",
            key_secret="supersecret123",
            workspace_id="workspace-abc",
            scope=["read", "write", "tasks:monitor"],
        )

    def test_validate_valid_key(self):
        """Valid API keys should pass validation."""
        is_valid, status, info = self.auth.validate_key(self.valid_key)
        assert is_valid is True
        assert status == KeyStatus.VALID
        assert info["key_id"] == "test-key-001"
        assert info["workspace_id"] == "workspace-abc"

    def test_validate_anonymous_key(self):
        """Anonymous/empty keys should be denied."""
        is_valid, status, info = self.auth.validate_key("")
        assert is_valid is False
        assert status == KeyStatus.ANONYMOUS

        is_valid, status, info = self.auth.validate_key("anonymous")
        assert is_valid is False
        assert status == KeyStatus.ANONYMOUS

        is_valid, status, info = self.auth.validate_key(None)
        assert is_valid is False
        assert status == KeyStatus.ANONYMOUS

    def test_validate_revoked_key(self):
        """Revoked API keys should be denied immediately."""
        # First, validate the key is valid
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is True

        # Revoke the key
        self.auth.revoke_key("test-key-001")

        # Now validation should fail
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is False
        assert status == KeyStatus.REVOKED

    def test_validate_disabled_key(self):
        """Disabled API keys should be denied."""
        # Disable the key
        self.auth.disable_key("test-key-001")

        # Validation should fail
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is False
        assert status == KeyStatus.DISABLED

    def test_validate_expired_key(self):
        """Expired API keys should be denied."""
        # Create a key that is already expired
        expired_key = self.auth.register_key(
            key_id="expired-key-001",
            key_secret="expired123",
            workspace_id="workspace-abc",
            scope=["read"],
            expires_at=time.time() - 100,  # Expired 100 seconds ago
        )

        is_valid, status, _ = self.auth.validate_key(expired_key)
        assert is_valid is False
        assert status == KeyStatus.EXPIRED

    def test_validate_key_with_insufficient_scope(self):
        """Keys without required scope should be denied."""
        # Create a key with limited scope
        limited_key = self.auth.register_key(
            key_id="limited-key-001",
            key_secret="limited123",
            workspace_id="workspace-abc",
            scope=["read"],  # No tasks:monitor scope
        )

        # Should fail when requiring tasks:monitor scope
        is_valid, status, info = self.auth.validate_key(
            limited_key,
            required_scope="tasks:monitor",
        )
        assert is_valid is False
        assert status == KeyStatus.INSUFFICIENT_SCOPE
        assert info["required_scope"] == "tasks:monitor"

        # But should pass for read scope
        is_valid, status, _ = self.auth.validate_key(
            limited_key,
            required_scope="read",
        )
        assert is_valid is True

    def test_revoked_key_rejected_during_long_poll_simulation(self):
        """
        Simulate long-polling: key valid at start, revoked mid-poll.
        
        This test verifies that even if a key is valid at the start of a
        long-poll operation, it will be rejected if revoked during the poll.
        """
        # Start of poll - key is valid
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is True

        # Simulate some time passing during long-poll
        time.sleep(0.01)

        # Key gets revoked (e.g., admin revokes it from another terminal)
        self.auth.revoke_key("test-key-001")

        # Next poll request - key must be revalidated and rejected
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is False
        assert status == KeyStatus.REVOKED

    def test_disabled_key_rejected_during_long_poll_simulation(self):
        """Same as above but for disabled keys."""
        # Start of poll - key is valid
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is True

        # Key gets disabled
        self.auth.disable_key("test-key-001")

        # Next poll - must be revalidated and rejected
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is False
        assert status == KeyStatus.DISABLED

    def test_enable_reenables_key(self):
        """Re-enabling a key should make it valid again."""
        self.auth.disable_key("test-key-001")
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is False

        self.auth.enable_key("test-key-001")
        is_valid, status, _ = self.auth.validate_key(self.valid_key)
        assert is_valid is True
        assert status == KeyStatus.VALID

    def test_key_hash_not_stored_in_plaintext(self):
        """Verify that the actual key secret is not stored."""
        key_info = self.auth.get_key_info("test-key-001")
        assert key_info is not None
        # The hash should not be the actual secret
        assert key_info.key_hash != "supersecret123"
        # And it should be a valid hash
        assert len(key_info.key_hash) == 64  # SHA256 hex length


class TestAuthServiceIntegration:
    """Integration tests for the global auth service singleton."""

    def setup_method(self):
        """Reset the global auth service."""
        set_auth_service(None)

    def teardown_method(self):
        """Clean up."""
        set_auth_service(None)

    def test_global_singleton(self):
        """Test that get_auth_service returns the same instance."""
        auth1 = get_auth_service()
        auth2 = get_auth_service()
        assert auth1 is auth2

    def test_set_auth_service(self):
        """Test that we can override the global auth service."""
        custom_auth = AuthService()
        set_auth_service(custom_auth)
        assert get_auth_service() is custom_auth


# 2026-05-21T08:00:00 update - Issue #625 fix tests
# Tests for API key revalidation during long-polling