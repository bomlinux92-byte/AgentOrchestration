## Summary
Fix for [BOUNTY $8k] [ Auth ] Validate auth scheme casing consistently — bearer parsing — Issue #3275

## Root cause
The AuthMiddleware was using a case-sensitive `token.startswith("Bearer ")` check, which meant that lowercase `bearer` or other casing variants would fail validation even though HTTP RFC 7235 requires case-insensitive scheme matching. This inconsistency could cause valid bearer tokens with non-standard casing to be rejected, and more critically, the guard logic was not validating that credentials existed after the scheme.

## Fix
Changed the bearer scheme validation in `src/api/middleware.py` from:
- `token.startswith("Bearer ")` (case-sensitive, no credential validation)

To:
- `scheme.lower() != "bearer" or not credentials.strip()` (case-insensitive scheme + empty credential check)

This ensures:
1. Bearer scheme is validated case-insensitively (Bearer, bearer, BEARER, etc.)
2. Credentials must be present and non-empty after the scheme

## Testing
Added `tests/test_middleware.py` with 13 test cases covering:
- Valid Bearer/BEARER/bearer scheme variations
- Invalid schemes (Basic, etc.) being rejected
- Missing/empty credentials being rejected
- Empty/missing Authorization headers being rejected
- Non-API v2 paths and /api/v2/auth/token endpoint being properly allowed

Closes #3275