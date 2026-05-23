## Summary
Fix for [BOUNTY $10k] Block disabled users from webhook management — Issue #1971

## Root cause
The AuthMiddleware only checked if a valid bearer token was present in the Authorization header, but did NOT verify whether the principal (user/integration) associated with that token was enabled, non-revoked, or non-expired. This allowed disabled/stale/revoked principals to bypass the auth check for webhook management endpoints (e.g., /api/v2/webhooks, /api/v2/integrations) as long as they had a structurally valid bearer token.

## Fix
1. Added DisabledPrincipalError and InvalidPrincipalError exception types to src/common/errors.py for precise error reporting.
2. Created src/common/webhook_auth.py with:
   - PrincipalState dataclass that tracks is_enabled, is_revoked, expires_at, and scopes
   - PrincipalStore for managing principal states (in-memory, replace with DB in production)
   - WebhookAuthGuard class that enforces RBAC checks before any protected webhook action
3. Updated src/api/middleware.py to:
   - Inject a WebhookAuthGuard instance
   - For webhook/integration management paths, call guard.check_principal() which validates the principal state BEFORE allowing the request to proceed
   - Return 403 for disabled principals, 401 for revoked/expired/invalid principals
4. Added comprehensive tests in tests/test_webhook_auth.py covering:
   - PrincipalState validation (disabled, revoked, expired, active)
   - PrincipalStore operations
   - WebhookAuthGuard checks
   - AuthMiddleware blocking disabled/revoked/expired/invalid principals from webhook endpoints

## Testing
All 28 new tests pass covering disabled, revoked, expired, and invalid principals being blocked from webhook management.

Closes #1971