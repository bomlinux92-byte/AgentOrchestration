"""API Key Store — Central permission service for API key validation and revocation checking."""

import threading
import time
from typing import Dict, Optional, Tuple


class ApiKeyStatus:
    ACTIVE = "active"
    REVOKED = "revoked"
    DISABLED = "disabled"
    EXPIRED = "expired"


class ApiKeyStore:
    """
    Central permission service for API key validation.
    Supports revocation checking and stale credential rejection.
    
    Thread-safe singleton store for API key metadata.
    In production, this would integrate with a database or external
    key management service (KMS). Here we provide an in-memory
    implementation suitable for testing and demonstration.
    """

    _instance: Optional["ApiKeyStore"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ApiKeyStore":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        # In-memory key metadata: key_id -> {status, owner_id, created_at, last_validated_at}
        self._keys: Dict[str, Dict] = {}
        self._rw_lock = threading.RLock()

    def register_key(
        self,
        key_id: str,
        owner_id: str,
        status: str = ApiKeyStatus.ACTIVE,
    ) -> None:
        """Register a new API key with its metadata."""
        with self._rw_lock:
            self._keys[key_id] = {
                "status": status,
                "owner_id": owner_id,
                "created_at": time.time(),
                "last_validated_at": time.time(),
            }

    def revoke_key(self, key_id: str) -> bool:
        """Revoke an API key. Returns True if the key was found and revoked."""
        with self._rw_lock:
            if key_id not in self._keys:
                return False
            self._keys[key_id]["status"] = ApiKeyStatus.REVOKED
            self._keys[key_id]["last_validated_at"] = time.time()
            return True

    def disable_key(self, key_id: str) -> bool:
        """Disable an API key. Returns True if the key was found and disabled."""
        with self._rw_lock:
            if key_id not in self._keys:
                return False
            self._keys[key_id]["status"] = ApiKeyStatus.DISABLED
            self._keys[key_id]["last_validated_at"] = time.time()
            return True

    def validate_key(self, key_id: str) -> Tuple[bool, Optional[str]]:
        """
        Validate an API key and refresh its last_validated_at timestamp.
        
        Returns:
            Tuple of (is_valid, error_message).
            is_valid=True means the key is active and not revoked/disabled.
            error_message describes the failure reason when is_valid=False.
        """
        with self._rw_lock:
            if key_id not in self._keys:
                return False, "API key not found"
            
            key_meta = self._keys[key_id]
            status = key_meta.get("status", ApiKeyStatus.ACTIVE)
            
            # Update last_validated_at
            key_meta["last_validated_at"] = time.time()
            
            if status == ApiKeyStatus.REVOKED:
                return False, "API key has been revoked"
            elif status == ApiKeyStatus.DISABLED:
                return False, "API key has been disabled"
            elif status == ApiKeyStatus.EXPIRED:
                return False, "API key has expired"
            elif status == ApiKeyStatus.ACTIVE:
                return True, None
            
            # Unknown status - treat as invalid
            return False, f"API key in unknown state: {status}"

    def is_valid(self, key_id: str) -> bool:
        """Quick check if a key is valid (active, not revoked/disabled)."""
        with self._rw_lock:
            if key_id not in self._keys:
                return False
            return self._keys[key_id].get("status") == ApiKeyStatus.ACTIVE

    def get_key_metadata(self, key_id: str) -> Optional[Dict]:
        """Get metadata for a key (read-only view)."""
        with self._rw_lock:
            if key_id not in self._keys:
                return None
            return dict(self._keys[key_id])

    def reset(self) -> None:
        """Clear all keys. Used for testing."""
        with self._rw_lock:
            self._keys.clear()
            self._initialized = False


# Global singleton instance
_api_key_store: Optional[ApiKeyStore] = None


def get_api_key_store() -> ApiKeyStore:
    """Get the global ApiKeyStore singleton."""
    global _api_key_store
    if _api_key_store is None:
        _api_key_store = ApiKeyStore()
    return _api_key_store