"""Tests for artifact download cache with checksum validation.

These tests cover:
- Cache storage with digest verification
- Cache retrieval with digest validation
- Digest mismatch detection and handling
- Partial file handling
- Corrupt cache entry eviction
- Thread-safe concurrent access
"""

import hashlib
import os
import shutil
import tempfile
import threading
import time
import pytest

from src.orchestrator.download_cache import (
    ArtifactDownloadCache,
    CacheState,
    CacheMetadata,
    DigestMismatchError,
    CacheCorruptError,
    get_download_cache,
    reset_download_cache,
)


class TestArtifactDownloadCache:
    def setup_method(self):
        self.cache_dir = tempfile.mkdtemp()
        self.cache = ArtifactDownloadCache(cache_dir=self.cache_dir)
    
    def teardown_method(self):
        if hasattr(self, "cache") and self.cache:
            self.cache.clear()
        if hasattr(self, "cache_dir") and os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
    
    def _make_digest(self, data):
        return hashlib.sha256(data).hexdigest()
    
    def test_put_and_get_artifact(self):
        artifact_id = "test-artifact-1"
        data = b"Hello, World!"
        digest = self._make_digest(data)
        assert self.cache.put(artifact_id, data, digest) is True
        retrieved = self.cache.get(artifact_id, expected_digest=digest)
        assert retrieved == data
    
    def test_get_nonexistent_returns_none(self):
        result = self.cache.get("nonexistent")
        assert result is None
    
    def test_digest_mismatch_raises_error(self):
        artifact_id = "test-artifact-2"
        data = b"Test data"
        correct_digest = self._make_digest(data)
        wrong_digest = self._make_digest(b"Wrong data")
        assert self.cache.put(artifact_id, data, correct_digest) is True
        with pytest.raises(DigestMismatchError):
            self.cache.get(artifact_id, expected_digest=wrong_digest)
    
    def test_has_valid_returns_true(self):
        artifact_id = "test-artifact-3"
        data = b"Test data"
        digest = self._make_digest(data)
        self.cache.put(artifact_id, data, digest)
        assert self.cache.has_valid(artifact_id, digest=digest) is True
    
    def test_has_valid_returns_false_wrong_digest(self):
        artifact_id = "test-artifact-4"
        data = b"Test data"
        correct_digest = self._make_digest(data)
        wrong_digest = self._make_digest(b"Wrong data")
        self.cache.put(artifact_id, data, correct_digest)
        assert self.cache.has_valid(artifact_id, digest=wrong_digest) is False
    
    def test_has_valid_returns_false_missing(self):
        assert self.cache.has_valid("nonexistent") is False
    
    def test_evict_removes_entry(self):
        artifact_id = "test-artifact-6"
        data = b"Test data"
        digest = self._make_digest(data)
        self.cache.put(artifact_id, data, digest)
        assert self.cache.has_valid(artifact_id) is True
        self.cache.evict(artifact_id)
        assert self.cache.has_valid(artifact_id) is False
    
    def test_get_without_digest_validates(self):
        artifact_id = "test-artifact-7"
        data = b"Test data"
        digest = self._make_digest(data)
        self.cache.put(artifact_id, data, digest)
        retrieved = self.cache.get(artifact_id)
        assert retrieved == data
    
    def test_corrupt_file_detection(self):
        artifact_id = "test-artifact-8"
        data = b"Test data"
        digest = self._make_digest(data)
        self.cache.put(artifact_id, data, digest)
        artifact_path = self.cache._get_artifact_path(artifact_id)
        with open(artifact_path, "wb") as f:
            f.write(b"Corrupted data!")
        with pytest.raises(DigestMismatchError):
            self.cache.get(artifact_id, expected_digest=digest)
    
    def test_evict_corrupt_removes_all(self):
        artifact_id_1 = "test-artifact-9a"
        artifact_id_2 = "test-artifact-9b"
        data = b"Test data"
        digest = self._make_digest(data)
        self.cache.put(artifact_id_1, data, digest)
        self.cache.put(artifact_id_2, data, digest)
        artifact_path = self.cache._get_artifact_path(artifact_id_1)
        with open(artifact_path, "wb") as f:
            f.write(b"Corrupted!")
        with pytest.raises(DigestMismatchError):
            self.cache.get(artifact_id_1, expected_digest=digest)
        count = self.cache.evict_corrupt()
        assert count >= 1
        assert self.cache.has_valid(artifact_id_1) is False
        assert self.cache.has_valid(artifact_id_2) is True


class TestPartialFileHandling:
    def setup_method(self):
        self.cache_dir = tempfile.mkdtemp()
        self.cache = ArtifactDownloadCache(cache_dir=self.cache_dir)
    
    def teardown_method(self):
        if hasattr(self, "cache") and self.cache:
            self.cache.clear()
        if hasattr(self, "cache_dir") and os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
    
    def _make_digest(self, data):
        return hashlib.sha256(data).hexdigest()
    
    def test_partial_file_not_returned(self):
        artifact_id = "test-partial-1"
        partial_path = self.cache._get_partial_path(artifact_id)
        with open(partial_path, "wb") as f:
            f.write(b"Partial content")
        assert self.cache.has_valid(artifact_id) is False
        assert self.cache.get(artifact_id) is None
    
    def test_complete_download_overwrites_partial(self):
        artifact_id = "test-partial-2"
        full_data = b"Complete content"
        digest = self._make_digest(full_data)
        partial_path = self.cache._get_partial_path(artifact_id)
        with open(partial_path, "wb") as f:
            f.write(b"Partial content")
        assert self.cache.put(artifact_id, full_data, digest) is True
        retrieved = self.cache.get(artifact_id, expected_digest=digest)
        assert retrieved == full_data
        assert partial_path.exists() is False


class TestThreadSafety:
    def setup_method(self):
        self.cache_dir = tempfile.mkdtemp()
        self.cache = ArtifactDownloadCache(cache_dir=self.cache_dir)
    
    def teardown_method(self):
        if hasattr(self, "cache") and self.cache:
            self.cache.clear()
        if hasattr(self, "cache_dir") and os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
    
    def _make_digest(self, data):
        return hashlib.sha256(data).hexdigest()
    
    def test_concurrent_put(self):
        errors = []
        results = []
        def put_artifact(artifact_id, data, digest):
            try:
                result = self.cache.put(artifact_id, data, digest)
                results.append((artifact_id, result))
            except Exception as e:
                errors.append((artifact_id, str(e)))
        threads = []
        for i in range(10):
            artifact_id = f"concurrent-{i}"
            data = f"Data {i}".encode()
            digest = self._make_digest(data)
            t = threading.Thread(target=put_artifact, args=(artifact_id, data, digest))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 10
        for i in range(10):
            assert self.cache.has_valid(f"concurrent-{i}") is True
    
    def test_concurrent_get_same_artifact(self):
        artifact_id = "shared-artifact"
        data = b"Shared data"
        digest = self._make_digest(data)
        self.cache.put(artifact_id, data, digest)
        results = []
        errors = []
        def get_artifact():
            try:
                result = self.cache.get(artifact_id, expected_digest=digest)
                results.append(result)
            except Exception as e:
                errors.append(str(e))
        threads = []
        for _ in range(10):
            t = threading.Thread(target=get_artifact)
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 10
        assert all(r == data for r in results)
    
    def test_start_download_blocks_others(self):
        artifact_id = "downloading-artifact"
        result1 = self.cache.start_download(artifact_id)
        assert result1 is True
        result2 = self.cache.start_download(artifact_id)
        assert result2 is False
        self.cache.end_download(artifact_id)
        result3 = self.cache.start_download(artifact_id)
        assert result3 is True


class TestCacheMetadata:
    def setup_method(self):
        self.cache_dir = tempfile.mkdtemp()
        self.cache = ArtifactDownloadCache(cache_dir=self.cache_dir)
    
    def teardown_method(self):
        if hasattr(self, "cache") and self.cache:
            self.cache.clear()
        if hasattr(self, "cache_dir") and os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
    
    def _make_digest(self, data):
        return hashlib.sha256(data).hexdigest()
    
    def test_metadata_persisted(self):
        artifact_id = "meta-test-1"
        data = b"Test data"
        digest = self._make_digest(data)
        self.cache.put(artifact_id, data, digest)
        new_cache = ArtifactDownloadCache(cache_dir=self.cache_dir)
        assert new_cache.has_valid(artifact_id, digest=digest) is True
        metadata = new_cache._metadata.get(artifact_id)
        assert metadata is not None
        assert metadata.digest == digest
        assert metadata.size == len(data)
    
    def test_list_entries_returns_all(self):
        for i in range(5):
            artifact_id = f"list-test-{i}"
            data = f"Data {i}".encode()
            digest = self._make_digest(data)
            self.cache.put(artifact_id, data, digest)
        entries = self.cache.list_entries()
        assert len(entries) == 5


class TestGlobalCache:
    def setup_method(self):
        reset_download_cache()
    
    def teardown_method(self):
        reset_download_cache()
    
    def test_get_download_cache_returns_same_instance(self):
        cache1 = get_download_cache(cache_dir="/tmp/test-cache-1")
        cache2 = get_download_cache(cache_dir="/tmp/test-cache-2")
        assert cache1 is cache2
    
    def test_reset_clears_global_cache(self):
        cache1 = get_download_cache(cache_dir="/tmp/test-cache-reset")
        reset_download_cache()
        cache2 = get_download_cache(cache_dir="/tmp/test-cache-reset")
        assert cache1 is not cache2


class TestDigestMismatchScenarios:
    def setup_method(self):
        self.cache_dir = tempfile.mkdtemp()
        self.cache = ArtifactDownloadCache(cache_dir=self.cache_dir)
    
    def teardown_method(self):
        if hasattr(self, "cache") and self.cache:
            self.cache.clear()
        if hasattr(self, "cache_dir") and os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
    
    def _make_digest(self, data):
        return hashlib.sha256(data).hexdigest()
    
    def test_truncated_file_detected(self):
        artifact_id = "truncated-test"
        full_data = b"This is the full content that should be cached"
        correct_digest = self._make_digest(full_data)
        self.cache.put(artifact_id, full_data, correct_digest)
        artifact_path = self.cache._get_artifact_path(artifact_id)
        with open(artifact_path, "wb") as f:
            f.write(b"Truncated")
        with pytest.raises(DigestMismatchError):
            self.cache.get(artifact_id, expected_digest=correct_digest)
    
    def test_extra_bytes_detected(self):
        artifact_id = "extra-bytes-test"
        original_data = b"Original content"
        correct_digest = self._make_digest(original_data)
        self.cache.put(artifact_id, original_data, correct_digest)
        artifact_path = self.cache._get_artifact_path(artifact_id)
        with open(artifact_path, "ab") as f:
            f.write(b"EXTRA")
        with pytest.raises(DigestMismatchError):
            self.cache.get(artifact_id, expected_digest=correct_digest)
