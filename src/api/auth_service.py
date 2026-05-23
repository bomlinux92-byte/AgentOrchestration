"""Authentication and authorization service."""

import time
from typing import Optional, Tuple
from enum import Enum


class TokenState(Enum):
    VALID = "valid"
    MISSING = "missing"
    MALFORMED = "malformed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    INSUFFICIENT_WORKSPACE_ROLE = "insufficient_workspace_role"
    WRONG_WORKSPACE = "wrong_workspace"


class Token:
    """Parsed bearer token with claims."""

    def __init__(
        self,
        raw: str,
        subject: Optional[str] = None,
        scope: Optional[str] = None,
        workspace_id: Optional[str] = None,
        expires_at: Optional[float] = None,
        issued_at: Optional[float] = None,
        revoked: bool = False,
    ):
        self.raw = raw
        self.subject = subject
        self.scope = scope or ""
        self.workspace_id = workspace_id
        self.expires_at = expires_at
        self.issued_at = issued_at
        self.revoked = revoked

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def has_scope(self, required: str) -> bool:
        if not self.scope:
            return False
        return required in self.scope.split()

    def to_dict(self):
        return {
            "subject": self.subject,
            "scope": self.scope,
            "workspace_id": self.workspace_id,
            "expires_at": self.expires_at,
            "issued_at": self.issued_at,
            "revoked": self.revoked,
        }


class APIPermissionService:
    """Central permission service for protected API routes."""

    def __init__(self):
        # In-memory revoked token registry for demonstration.
        # Replace with Redis/DB lookup in production.
        self._revoked: set = set()
        # In-memory token cache: token_raw -> Token
        self._token_cache: dict = {}

    def parse_token(self, authorization: str) -> Tuple[Optional[Token], TokenState]:
        """Parse a Bearer token from the Authorization header value."""
        if not authorization:
            return None, TokenState.MISSING

        if not authorization.startswith("Bearer "):
            return None, TokenState.MALFORMED

        raw = authorization[len("Bearer ") :].strip()
        if not raw:
            return None, TokenState.MALFORMED

        # Check revocation list
        if raw in self._revoked:
            return None, TokenState.REVOKED

        # Check cache or create a mock token for existing tests
        if raw in self._token_cache:
            token = self._token_cache[raw]
        else:
            # Parse mock JWT-like structure: sub|scope|workspace|exp|iat
            # This is for demonstration; real implementation would decode JWT
            parts = raw.split(".")
            if len(parts) == 3:
                import base64, json
                try:
                    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
                    token = Token(
                        raw=raw,
                        subject=payload.get("sub"),
                        scope=payload.get("scope"),
                        workspace_id=payload.get("workspace_id"),
                        expires_at=payload.get("exp"),
                        issued_at=payload.get("iat"),
                        revoked=False,
                    )
                except Exception:
                    token = Token(raw=raw)
            else:
                # Simple token format for tests: subject_scope_workspace_exp_iat
                # e.g. "user1:read:ws1:9999999999:0"
                try:
                    comps = raw.split("_")
                    if len(comps) >= 3:
                        token = Token(
                            raw=raw,
                            subject=comps[0],
                            scope=comps[1] if len(comps) > 1 else "",
                            workspace_id=comps[2] if len(comps) > 2 else None,
                            expires_at=float(comps[3]) if len(comps) > 3 and comps[3] else None,
                            issued_at=float(comps[4]) if len(comps) > 4 and comps[4] else None,
                        )
                    else:
                        token = Token(raw=raw, subject=raw)
                except Exception:
                    token = Token(raw=raw)

            self._token_cache[raw] = token

        return token, TokenState.VALID

    def revoke(self, raw: str) -> None:
        """Revoke a token."""
        self._revoked.add(raw)
        if raw in self._token_cache:
            self._token_cache[raw].revoked = True

    def check_permission(
        self,
        token: Token,
        required_scope: Optional[str] = None,
        required_workspace_role: Optional[str] = None,
        target_workspace_id: Optional[str] = None,
    ) -> TokenState:
        """Check if a token has the required permissions."""
        if token.is_expired():
            return TokenState.EXPIRED
        if token.revoked:
            return TokenState.REVOKED
        if required_scope and not token.has_scope(required_scope):
            return TokenState.INSUFFICIENT_SCOPE
        if required_workspace_role and not self._check_workspace_role(token, required_workspace_role):
            return TokenState.INSUFFICIENT_WORKSPACE_ROLE
        if target_workspace_id and token.workspace_id != target_workspace_id:
            return TokenState.WRONG_WORKSPACE
        return TokenState.VALID

    def _check_workspace_role(self, token: Token, required: str) -> bool:
        """Check if token has the required workspace role."""
        if not token.workspace_id:
            return False
        # Token subject format: user:role or just user
        if ":" in (token.subject or ""):
            _, role = token.subject.split(":", 1)
            return role == required
        # Default: anyone in the workspace with a valid token can access
        return True


# Global singleton
_permission_service = APIPermissionService()


def get_permission_service() -> APIPermissionService:
    return _permission_service