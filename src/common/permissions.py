"""Permission service for workspace/project role enforcement on secret metadata API."""

import time
import logging
from typing import Dict, Optional, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Principal:
    """Represents an authenticated principal with workspace and role context."""
    principal_id: str
    workspace_id: str
    role: str
    scopes: Set[str] = field(default_factory=set)
    is_machine_token: bool = False
    issued_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    is_revoked: bool = False


class PermissionService:
    """
    Central permission service that enforces workspace/project roles on
    environment variable reads (secret metadata API).

    Validates that:
    - The principal is not anonymous
    - The principal's token is not stale or expired
    - The principal is not revoked
    - The principal has sufficient scopes for the requested action
    - The principal's workspace matches the resource's workspace
    """

    # Role hierarchy: higher roles include permissions of lower roles
    ROLE_HIERARCHY = {
        "admin": {"read", "write", "delete", "admin"},
        "editor": {"read", "write"},
        "viewer": {"read"},
    }

    def __init__(self):
        # In-memory token store for revocation checks (in production, use Redis)
        self._revoked_tokens: Set[str] = set()
        self._principal_cache: Dict[str, Principal] = {}

    def validate_principal(self, principal: Principal) -> bool:
        """
        Validate that a principal is allowed to make requests.
        Returns False if: anonymous, stale, expired, or revoked.
        """
        # Check for anonymous principal
        if not principal.principal_id or principal.principal_id == "anonymous":
            logger.warning("Anonymous principal denied")
            return False

        # Check if token is revoked
        if principal.is_revoked or principal.principal_id in self._revoked_tokens:
            logger.warning(f"Revoked principal denied: {principal.principal_id}")
            return False

        # Check expiration
        if principal.expires_at is not None and time.time() > principal.expires_at:
            logger.warning(f"Expired principal denied: {principal.principal_id}")
            return False

        # Check staleness (tokens older than 24 hours considered stale)
        staleness_threshold = 24 * 60 * 60  # 24 hours
        if time.time() - principal.issued_at > staleness_threshold:
            logger.warning(f"Stale principal denied: {principal.principal_id}")
            return False

        return True

    def check_permission(
        self,
        principal: Principal,
        workspace_id: str,
        required_scope: str = "read",
    ) -> bool:
        """
        Check if principal has the required permission for the workspace.

        Enforces:
        - Principal must be valid (not anonymous/stale/revoked)
        - Principal's workspace must match the resource workspace
        - Principal must have the required scope in their role
        """
        if not self.validate_principal(principal):
            return False

        # Enforce workspace scope
        if principal.workspace_id != workspace_id:
            logger.warning(
                f"Workspace mismatch: principal {principal.principal_id} "
                f"in workspace {principal.workspace_id}, "
                f"requested workspace {workspace_id}"
            )
            return False

        # Check role-based scopes
        role = principal.role.lower()
        allowed_scopes = self.ROLE_HIERARCHY.get(role, set())

        if required_scope not in allowed_scopes:
            logger.warning(
                f"Insufficient scope: principal {principal.principal_id} "
                f"with role {role} lacks scope {required_scope}"
            )
            return False

        return True

    def check_env_var_read(
        self,
        principal: Principal,
        workspace_id: str,
    ) -> bool:
        """
        Enforce project role on environment variable reads (secret metadata API).
        This is the primary entry point for the secret metadata API authorization.
        """
        return self.check_permission(principal, workspace_id, required_scope="read")

    def revoke_principal(self, principal_id: str) -> None:
        """Revoke a principal, immediately denying all their requests."""
        self._revoked_tokens.add(principal_id)
        logger.info(f"Principal revoked: {principal_id}")

    def get_or_create_principal(
        self,
        principal_id: str,
        workspace_id: str,
        role: str,
        scopes: Optional[Set[str]] = None,
        is_machine_token: bool = False,
        expires_at: Optional[float] = None,
    ) -> Principal:
        """Get cached principal or create a new one."""
        cache_key = f"{principal_id}:{workspace_id}"
        if cache_key in self._principal_cache:
            return self._principal_cache[cache_key]

        principal = Principal(
            principal_id=principal_id,
            workspace_id=workspace_id,
            role=role,
            scopes=scopes or set(),
            is_machine_token=is_machine_token,
            expires_at=expires_at,
        )
        self._principal_cache[cache_key] = principal
        return principal


# Global singleton instance
_permission_service: Optional[PermissionService] = None


def get_permission_service() -> PermissionService:
    """Get the global permission service singleton."""
    global _permission_service
    if _permission_service is None:
        _permission_service = PermissionService()
    return _permission_service
