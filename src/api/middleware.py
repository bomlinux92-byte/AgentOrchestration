"""API middleware components."""

import time
import logging
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from .auth_service import get_permission_service, TokenState

logger = logging.getLogger(__name__)

# Protected route prefixes — require full authentication and authorization
PROTECTED_ROUTE_PREFIXES = ("/api/v2/agents", "/api/v2/config", "/api/v2/scheduler")


class AuthMiddleware(BaseHTTPMiddleware):
    """Auth middleware that fails closed on protected routes.

    Enforces the check in the central dependency before protected route handlers
    return data, mutate state, dispatch work, or record success.
    Specifically covers the 'Prevent auth bypass via trailing slash redirect'
    condition for protected routes.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Strip trailing slash to prevent redirect-based bypass
        # FastAPI allows /route/ and /route to be different routes;
        # redirecting causes the browser to follow with the trailing slash,
        # potentially shifting the protected path before authorization is checked.
        if path != "/" and path.endswith("/"):
            # Normalize: redirect to non-trailing version would leak the normalized path.
            # Instead, reject trailing-slash requests on protected routes outright.
            normalized = path.rstrip("/")
            if any(path.startswith(prefix) for prefix in PROTECTED_ROUTE_PREFIXES):
                # Reject outright — do not redirect, which could expose the normalized path
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Trailing slash not permitted on protected routes"},
                )

        # Only enforce auth on protected routes
        if not any(path.startswith(prefix) for prefix in PROTECTED_ROUTE_PREFIXES):
            return await call_next(request)

        # Extract Authorization header
        authorization = request.headers.get("Authorization", "")
        ps = get_permission_service()

        # Parse token
        token, state = ps.parse_token(authorization)
        if state != TokenState.VALID:
            logger.warning(f"Auth failed for {path}: {state.value} from {request.client.host}")
            return JSONResponse(status_code=401, content={"detail": state.value})

        # Check permission for agent registration workflow
        # (authorized operators with correct workspace role complete successfully)
        if token is not None:
            permission_state = ps.check_permission(
                token,
                required_scope="agent:write",
                required_workspace_role="operator",
            )
            # Auth-related failures (expired, revoked) map to 401; permission failures to 403
            if permission_state != TokenState.VALID:
                if permission_state in (TokenState.EXPIRED, TokenState.REVOKED):
                    logger.warning(f"Auth failed for {path}: {permission_state.value} from {request.client.host}")
                    return JSONResponse(status_code=401, content={"detail": permission_state.value})
                logger.warning(
                    f"Permission denied for {path}: {permission_state.value} "
                    f"subject={token.subject} workspace={token.workspace_id}"
                )
                return JSONResponse(status_code=403, content={"detail": permission_state.value})

        # Store token in request state for downstream handlers
        request.state.token = token

        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 100, window: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window
        self._requests = {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        if client_ip not in self._requests:
            self._requests[client_ip] = []

        self._requests[client_ip] = [t for t in self._requests[client_ip] if now - t < self.window]

        if len(self._requests[client_ip]) >= self.max_requests:
            return Response(status_code=429, content="Too many requests")

        self._requests[client_ip].append(now)
        return await call_next(request)


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        logger.info(f"{request.method} {request.url.path} {response.status_code} {duration:.3f}s")
        return response