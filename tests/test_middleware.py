import pytest
import gzip
import zlib
import sys
import os

# Add src to path for direct imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response

from src.api.middleware import BodyMiddleware


def create_app(middleware_cls, **middleware_kwargs):
    async def endpoint(request: Request) -> Response:
        body = await request.body()
        return Response(content=f"received {len(body)} bytes")

    app = Starlette(
        routes=[],
        middleware=[Middleware(middleware_cls, **middleware_kwargs)],
    )
    app.add_route("/test", endpoint, methods=["POST"])
    return app


class TestBodyMiddlewareGzipBombPrevention:
    """Test suite for gzip bomb prevention in BodyMiddleware."""

    def test_normal_request_passes_through(self):
        """Normal uncompressed request should pass through without issues."""
        app = create_app(BodyMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post("/test", content=b"hello world", headers={})
        assert response.status_code == 200

    def test_normal_gzip_request_passes_through(self):
        """Normal gzip-compressed request within limits should pass through."""
        app = create_app(BodyMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        data = b"hello world" * 100  # small compressed data
        compressed = gzip.compress(data)
        response = client.post(
            "/test",
            content=compressed,
            headers={"Content-Encoding": "gzip"},
        )
        assert response.status_code == 200

    def test_compressed_size_limit_rejected(self):
        """Request with compressed body exceeding limit should be rejected."""
        app = create_app(BodyMiddleware, max_compressed_size=100)
        client = TestClient(app, raise_server_exceptions=False)
        data = b"x" * 200
        compressed = gzip.compress(data)
        response = client.post(
            "/test",
            content=compressed,
            headers={"Content-Encoding": "gzip"},
        )
        assert response.status_code == 413
        assert "compressed body exceeds limit" in response.text

    def test_decompressed_size_limit_rejected(self):
        """Request with decompressed body exceeding limit should be rejected."""
        app = create_app(BodyMiddleware, max_decompressed_size=50)
        client = TestClient(app, raise_server_exceptions=False)
        # 100 bytes decompressed
        data = b"x" * 100
        compressed = gzip.compress(data)
        response = client.post(
            "/test",
            content=compressed,
            headers={"Content-Encoding": "gzip"},
        )
        assert response.status_code == 413
        assert "decompressed body exceeds limit" in response.text

    def test_compression_ratio_limit_rejected(self):
        """Request with extreme compression ratio should be rejected (gzip bomb)."""
        app = create_app(BodyMiddleware, max_compression_ratio=10.0)
        client = TestClient(app, raise_server_exceptions=False)
        # Very high ratio: 1 byte compresses to much larger decompressed
        # Create a payload that has extreme compression ratio
        data = b"\x00" * 10000  # highly compressible
        compressed = gzip.compress(data)
        ratio = len(data) / max(len(compressed), 1)
        # Should have high ratio
        assert ratio > 10, f"Expected ratio > 10, got {ratio}"

        response = client.post(
            "/test",
            content=compressed,
            headers={"Content-Encoding": "gzip"},
        )
        assert response.status_code == 413
        assert "compression ratio exceeds" in response.text

    def test_deflate_encoding_accepted(self):
        """Deflate-encoded requests should also be protected."""
        app = create_app(BodyMiddleware, max_compressed_size=100)
        client = TestClient(app, raise_server_exceptions=False)
        data = b"x" * 50
        compressed = zlib.compress(data)
        response = client.post(
            "/test",
            content=compressed,
            headers={"Content-Encoding": "deflate"},
        )
        assert response.status_code == 200

    def test_content_length_header_precheck(self):
        """Content-Length header should be checked before reading body."""
        app = create_app(BodyMiddleware, max_compressed_size=100)
        client = TestClient(app, raise_server_exceptions=False)
        data = b"x" * 200
        compressed = gzip.compress(data)
        response = client.post(
            "/test",
            content=compressed,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
        )
        assert response.status_code == 413

    def test_no_request_state_leakage(self):
        """Request-local state should not leak between requests."""
        app = create_app(BodyMiddleware)
        client = TestClient(app, raise_server_exceptions=False)

        # First request: normal
        response1 = client.post("/test", content=b"hello")
        assert response1.status_code == 200

        # Second request: should not be affected by first request
        response2 = client.post("/test", content=b"world")
        assert response2.status_code == 200

    def test_invalid_content_encoding_ignored(self):
        """Unknown content encodings should pass through (no protection needed)."""
        app = create_app(BodyMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/test",
            content=b"some data",
            headers={"Content-Encoding": "br"},  # brotli - not handled
        )
        # Should pass through to handler
        assert response.status_code == 200

    def test_empty_body_accepted(self):
        """Empty compressed body should be handled gracefully."""
        app = create_app(BodyMiddleware)
        client = TestClient(app, raise_server_exceptions=False)
        compressed = gzip.compress(b"")
        response = client.post(
            "/test",
            content=compressed,
            headers={"Content-Encoding": "gzip"},
        )
        assert response.status_code == 200

    def test_exception_path_releases_state(self):
        """Exceptions during processing should not leak state."""
        app = create_app(BodyMiddleware)
        client = TestClient(app, raise_server_exceptions=False)

        # Make a request that triggers error path
        data = b"x" * 200
        compressed = gzip.compress(data)
        response = client.post(
            "/test",
            content=compressed,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
        )
        # Should be rejected, not crash
        assert response.status_code == 413

        # Next request should work normally
        response2 = client.post("/test", content=b"normal request")
        assert response2.status_code == 200


# 2026-05-20T02:10:00 update