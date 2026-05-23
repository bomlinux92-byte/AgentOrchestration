"""Webhook authentication and RBAC guard.

Enforces that disabled, revoked, or expired principals are blocked from
webhook management operations before any protected action is performed.
"""

import time
import logging
from typing import Optional, Dict, Any, Set
from dataclasses import dataclass, field

from src.common.errors import DisabledPrincipalError, InvalidPrincipalError

logger = logging.getLogger(__name__)


@dataclass
class PrincipalState:
    """Represents the authorization state of a principal (user/integration)."""
    principal_id: str
    is_enabled: bool = True
    is_revoked: bool = False
    expires_at: Optional[float] = None  # Unix timestamp, None = never expires
    scopes: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_active(self) -> bool:
        """Check if the principal is currently active (not disabled/revoked/expired)."""
        if not self.is_enabled or self.is_revoked:
            return False
        if self.expires_at is not None and time.time() > self.expires_at:
            return False
        return True

    def validate(self) -> None:
        """Validate principal state, raising appropriate exception if invalid."""
        if not self.is_enabled:
            raise DisabledPrincipalError(
                principal_id=self.principal_id,
                reason="principal account is disabled"
            )
        if self.is_revoked:
            raise InvalidPrincipalError(
                principal_id=self.principal_id,
                reason="principal credentials have been revoked"
            )
        if self.expires_at is not None and time.time() > self.expires_at:
            raise InvalidPrincipalError(
                principal_id=self.principal_id,
                reason="principal credentials have expired"
            )


class PrincipalStore:
    """In-memory store of principal states (replace with DB in production)."""

    def __init__(self):
        self._principals: Dict[str, PrincipalState] = {}

    def get(self, principal_id: str) -> Optional[PrincipalState]:
        return self._principals.get(principal_id)

    def register(
        self,
        principal_id: str,
        is_enabled: bool = True,
        expires_at: Optional[float] = None,
        scopes: Optional[Set[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PrincipalState:
        state = PrincipalState(
            principal_id=principal_id,
            is_enabled=is_enabled,
            expires_at=expires_at,
            scopes=scopes or set(),
            metadata=metadata or {},
        )
        self._principals[principal_id] = state
        return state

    def set_enabled(self, principal_id: str, enabled: bool) -> bool:
        """Enable or disable a principal."""
        if principal_id not in self._principals:
            return False
        self._principals[principal_id].is_enabled = enabled
        return True

    def revoke(self, principal_id: str) -> bool:
        """Revoke a principal's credentials."""
        if principal_id not in self._principals:
            return False
        self._principals[principal_id].is_revoked = True
        return True

    def remove(self, principal_id: str) -> bool:
        """Remove a principal from the store."""
        if principal_id in self._principals:
            del self._principals[principal_id]
            return True
        return False


# Global principal store instance
_principal_store: Optional[PrincipalStore] = None


def get_principal_store() -> PrincipalStore:
    """Get the global principal store, creating it if needed."""
    global _principal_store
    if _principal_store is None:
        _principal_store = PrincipalStore()
    return _principal_store


def reset_principal_store() -> None:
    """Reset the global principal store (for testing)."""
    global _principal_store
    _principal_store = None


class WebhookAuthGuard:
    """RBAC guard for webhook management operations.

    Enforces that only active, enabled principals can perform webhook
    management operations. All validation happens before any protected
    action is executed.
    """

    # Scopes required for webhook management
    WEBHOOK_SCOPES = {"webhook:read", "webhook:write", "webhook:manage"}

    def __init__(self, principal_store: Optional[PrincipalStore] = None):
        self.store = principal_store or get_principal_store()

    def check_principal(self, principal_id: str) -> PrincipalState:
        """Retrieve and validate a principal, raising if invalid."""
        state = self.store.get(principal_id)
        if state is None:
            raise InvalidPrincipalError(
                principal_id=principal_id,
                reason="principal not found"
            )
        state.validate()
        return state

    def can_manage_webhooks(self, principal_id: str) -> bool:
        """Check if a principal can manage webhooks (has at least one webhook scope)."""
        try:
            state = self.check_principal(principal_id)
            return bool(state.scopes & self.WEBHOOK_SCOPES)
        except (DisabledPrincipalError, InvalidPrincipalError):
            return False

    def assert_can_manage_webhooks(self, principal_id: str) -> None:
        """Assert that a principal can manage webhooks, raising if not authorized."""
        state = self.check_principal(principal_id)
        if not state.scopes & self.WEBHOOK_SCOPES:
            raise InvalidPrincipalError(
                principal_id=principal_id,
                reason="missing webhook management scopes"
            )

    def invalidate_principal(self, principal_id: str) -> None:
        """Invalidate a principal by disabling and revoking them."""
        self.store.set_enabled(principal_id, False)
        self.store.revoke(principal_id)
        logger.info("Invalidated principal: %s", principal_id)


# Default guard instance
_default_guard: Optional[WebhookAuthGuard] = None


def get_webhook_auth_guard() -> WebhookAuthGuard:
    """Get the default webhook auth guard."""
    global _default_guard
    if _default_guard is None:
        _default_guard = WebhookAuthGuard()
    return _default_guard


def reset_webhook_auth_guard() -> None:
    """Reset the default guard (for testing)."""
    global _default_guard
    _default_guard = None
    reset_principal_store()