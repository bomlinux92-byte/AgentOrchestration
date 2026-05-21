"""Authentication and API key validation service."""

import time
import hashlib
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class KeyStatus(Enum):
    """API key status states."""
    VALID = "valid"
    REVOKED = "revoked"
    DISABLED = "disabled"
    EXPIRED = "expired"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    ANONYMOUS = "anonymous"


@dataclass
class APIKeyInfo:
    """Information about an API key."""
    key_id: str
    key_hash: str
    workspace_id: str
    status: KeyStatus
    scope: List[str]
    created_at: float
    expires_at: Optional[float] = None
    revoked_at: Optional[float] = None


class AuthService:
    """
    Central authentication and authorization service.
    
    Validates API keys on every request to ensure revoked/disabled/expired
    keys are immediately rejected, even during long-polling operations.
    """
    
    def __init__(self):
        # In production, this would be backed by a database
        self._keys: Dict[str, APIKeyInfo] = {}
        self._revoked_keys: Dict[str, float] = {}  # key_id -> revoked_at
    
    def register_key(
        self,
        key_id: str,
        key_secret: str,
        workspace_id: str,
        scope: List[str],
        expires_at: Optional[float] = None,
    ) -> str:
        """
        Register a new API key.
        Returns the full API key (only shown once).
        """
        key_hash = self._hash_key(key_secret)
        
        info = APIKeyInfo(
            key_id=key_id,
            key_hash=key_hash,
            workspace_id=workspace_id,
            status=KeyStatus.VALID,
            scope=scope,
            created_at=time.time(),
            expires_at=expires_at,
        )
        self._keys[key_id] = info
        return f"{key_id}:{key_secret}"
    
    def revoke_key(self, key_id: str) -> bool:
        """Revoke an API key immediately."""
        if key_id in self._keys:
            self._keys[key_id].status = KeyStatus.REVOKED
            self._keys[key_id].revoked_at = time.time()
            self._revoked_keys[key_id] = time.time()
            logger.info(f"API key {key_id} revoked")
            return True
        return False
    
    def disable_key(self, key_id: str) -> bool:
        """Disable an API key."""
        if key_id in self._keys:
            self._keys[key_id].status = KeyStatus.DISABLED
            logger.info(f"API key {key_id} disabled")
            return True
        return False
    
    def enable_key(self, key_id: str) -> bool:
        """Re-enable a disabled API key."""
        if key_id in self._keys:
            self._keys[key_id].status = KeyStatus.VALID
            logger.info(f"API key {key_id} enabled")
            return True
        return False
    
    def validate_key(
        self,
        key_secret: str,
        required_scope: Optional[str] = None,
    ) -> Tuple[bool, KeyStatus, Optional[Dict]]:
        """
        Validate an API key on every request.
        
        This should be called on EVERY request, not just at connection time,
        especially important for long-polling endpoints.
        
        Args:
            key_secret: The full API key (key_id:key_secret format)
            required_scope: Optional scope required for this operation
            
        Returns:
            Tuple of (is_valid, status, key_info)
        """
        if not key_secret or key_secret == "anonymous":
            return False, KeyStatus.ANONYMOUS, None
        
        # Parse key_id:key_secret format
        if ":" not in key_secret:
            return False, KeyStatus.ANONYMOUS, None
        
        key_id, secret = key_secret.split(":", 1)
        key_hash = self._hash_key(secret)
        
        # Look up the key
        key_info = self._keys.get(key_id)
        if not key_info:
            return False, KeyStatus.ANONYMOUS, None
        
        # Check if key hash matches
        if key_info.key_hash != key_hash:
            return False, KeyStatus.ANONYMOUS, None
        
        # Check status
        if key_info.status == KeyStatus.REVOKED:
            logger.warning(f"Revoked key {key_id} used")
            return False, KeyStatus.REVOKED, None
        
        if key_info.status == KeyStatus.DISABLED:
            logger.warning(f"Disabled key {key_id} used")
            return False, KeyStatus.DISABLED, None
        
        # Check expiration
        if key_info.expires_at and time.time() > key_info.expires_at:
            key_info.status = KeyStatus.EXPIRED
            logger.warning(f"Expired key {key_id} used")
            return False, KeyStatus.EXPIRED, None
        
        # Check scope if required
        if required_scope and required_scope not in key_info.scope:
            logger.warning(f"Key {key_id} lacks required scope {required_scope}")
            return False, KeyStatus.INSUFFICIENT_SCOPE, {
                "key_id": key_id,
                "workspace_id": key_info.workspace_id,
                "required_scope": required_scope,
                "provided_scopes": key_info.scope,
            }
        
        return True, KeyStatus.VALID, {
            "key_id": key_id,
            "workspace_id": key_info.workspace_id,
            "scope": key_info.scope,
        }
    
    def get_key_info(self, key_id: str) -> Optional[APIKeyInfo]:
        """Get information about a key (without secrets)."""
        return self._keys.get(key_id)
    
    @staticmethod
    def _hash_key(key: str) -> str:
        """Hash an API key for storage."""
        return hashlib.sha256(key.encode()).hexdigest()


# Global auth service instance
_auth_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    """Get the global auth service instance."""
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service


def set_auth_service(service: AuthService) -> None:
    """Set the global auth service instance (for testing)."""
    global _auth_service
    _auth_service = service


# 2026-05-21T08:00:00 update - Initial implementation for issue #625
# Long polling API key revalidation