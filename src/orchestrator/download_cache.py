"""Artifact Download Cache — Provides checksum-validated local cache for artifact consumers.

This module ensures:
1. Cache hits verify content digest before use (SHA-256 by default)
2. Corrupt cache entries are evicted and marked for re-download
3. Thread-safe concurrent access to cached artifacts
4. Atomic write operations to prevent partial cache corruption

Acceptance Criteria:
- Cache hits verify content digest before use
- Corrupt cache entries are evicted and re-downloaded
- Tests cover partial files and digest mismatches
"""

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Set
from uuid import uuid4

logger = logging.getLogger(__name__)


class CacheState(Enum):
    """Cache entry lifecycle states."""
    VALID = "valid"
    CORRUPT = "corrupt"
    DOWNLOADING = "downloading"
    EVICTED = "evicted"


@dataclass
class CacheMetadata:
    """Metadata stored with each cached artifact.
    
    Stores the expected digest for integrity verification.
    """
    artifact_id: str
    digest: str
    size: int
    created_at: float
    last_verified: float
    state: CacheState = CacheState.VALID
    download_url: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            "artifact_id": self.artifact_id,
            "digest": self.digest,
            "size": self.size,
            "created_at": self.created_at,
            "last_verified": self.last_verified,
            "state": self.state.value,
            "download_url": self.download_url,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "CacheMetadata":
        return cls(
            artifact_id=data["artifact_id"],
            digest=data["digest"],
            size=data["size"],
            created_at=data["created_at"],
            last_verified=data["last_verified"],
            state=CacheState(data["state"]),
            download_url=data.get("download_url"),
        )


class DigestMismatchError(Exception):
    """Raised when cached artifact digest doesn't match expected digest."""
    pass


class CacheCorruptError(Exception):
    """Raised when cache entry is detected as corrupt."""
    pass


class ArtifactDownloadCache:
    """Local download cache with checksum validation for artifact consumers.
    
    This cache:
    1. Stores artifacts with their expected SHA-256 digests
    2. Validates digest before returning cache hits
    3. Evicts corrupt entries and marks them for re-download
    4. Provides thread-safe concurrent access
    
    Usage:
        cache = ArtifactDownloadCache(cache_dir="/var/cache/artifacts")
        
        # Store a downloaded artifact
        cache.put(artifact_id="artifact-123", data=bytes_data, digest="sha256:...")
        
        # Retrieve with validation (raises on corrupt/missing)
        data = cache.get(artifact_id="artifact-123", expected_digest="sha256:...")
        
        # Check if valid cache exists
        if cache.has_valid(artifact_id="artifact-123", digest="sha256:..."):
            data = cache.get(artifact_id="artifact-123", expected_digest="sha256:...")
    """
    
    DEFAULT_DIGEST_ALGO = "sha256"
    METADATA_SUFFIX = ".meta.json"
    PARTIAL_SUFFIX = ".partial"
    
    def __init__(
        self,
        cache_dir: str,
        digest_algorithm: str = DEFAULT_DIGEST_ALGO,
        max_size_mb: Optional[int] = None,
    ):
        """Initialize the artifact download cache.
        
        Args:
            cache_dir: Base directory for cache storage
            digest_algorithm: Hash algorithm for digest validation (default: sha256)
            max_size_mb: Optional maximum cache size in MB for eviction policy
        """
        self.cache_dir = Path(cache_dir)
        self.digest_algorithm = digest_algorithm
        self.max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb else None
        
        self._metadata: Dict[str, CacheMetadata] = {}
        self._lock = threading.RLock()
        self._downloading: Set[str] = set()  # Track entries being downloaded
        
        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Load existing metadata
        self._load_metadata()
    
    def _get_artifact_path(self, artifact_id: str) -> Path:
        """Get the file path for an artifact."""
        return self.cache_dir / artifact_id
    
    def _get_metadata_path(self, artifact_id: str) -> Path:
        """Get the metadata file path for an artifact."""
        return self.cache_dir / f"{artifact_id}{self.METADATA_SUFFIX}"
    
    def _get_partial_path(self, artifact_id: str) -> Path:
        """Get the partial file path for an artifact being downloaded."""
        return self.cache_dir / f"{artifact_id}{self.PARTIAL_SUFFIX}"
    
    def _compute_digest(self, data: bytes) -> str:
        """Compute digest of data using configured algorithm."""
        hash_obj = hashlib.new(self.digest_algorithm)
        hash_obj.update(data)
        return hash_obj.hexdigest()
    
    def _load_metadata(self) -> None:
        """Load metadata for all cached artifacts."""
        with self._lock:
            for meta_file in self.cache_dir.glob(f"*{self.METADATA_SUFFIX}"):
                try:
                    artifact_id = meta_file.name[:-len(self.METADATA_SUFFIX)]
                    with open(meta_file, 'r') as f:
                        data = json.load(f)
                    self._metadata[artifact_id] = CacheMetadata.from_dict(data)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(f"Failed to load metadata for {meta_file}: {e}")
    
    def _save_metadata(self, metadata: CacheMetadata) -> None:
        """Save metadata for an artifact."""
        meta_path = self._get_metadata_path(metadata.artifact_id)
        with open(meta_path, 'w') as f:
            json.dump(metadata.to_dict(), f)
    
    def _verify_digest(self, data: bytes, expected_digest: str) -> bool:
        """Verify data digest matches expected digest."""
        actual_digest = self._compute_digest(data)
        return actual_digest == expected_digest
    
    def put(
        self,
        artifact_id: str,
        data: bytes,
        digest: str,
        download_url: Optional[str] = None,
    ) -> bool:
        """Store artifact data in cache with digest for future validation.
        
        This writes to a partial file first, then renames to the final location
        to prevent partial write corruption.
        
        Args:
            artifact_id: Unique identifier for the artifact
            data: Raw artifact bytes
            digest: Expected SHA-256 digest of the artifact
            download_url: Optional URL for re-download if needed
        
        Returns:
            True if stored successfully, False if write failed
        """
        artifact_path = self._get_artifact_path(artifact_id)
        partial_path = self._get_partial_path(artifact_id)
        
        with self._lock:
            try:
                # Write to temp partial file atomically
                with tempfile.NamedTemporaryFile(
                    dir=self.cache_dir,
                    delete=False,
                    prefix=f".{artifact_id}",
                    suffix=self.PARTIAL_SUFFIX,
                ) as tmp:
                    tmp.write(data)
                    tmp_path = Path(tmp.name)
                
                # Verify the data we're about to cache
                if not self._verify_digest(data, digest):
                    tmp_path.unlink(missing_ok=True)
                    logger.error(f"Digest verification failed for {artifact_id}")
                    return False
                
                # Rename to final location (atomic on POSIX)
                tmp_path.rename(partial_path)
                
                # Update metadata
                import time
                metadata = CacheMetadata(
                    artifact_id=artifact_id,
                    digest=digest,
                    size=len(data),
                    created_at=time.time(),
                    last_verified=time.time(),
                    state=CacheState.VALID,
                    download_url=download_url,
                )
                self._save_metadata(metadata)
                self._metadata[artifact_id] = metadata
                
                # Rename to final artifact path
                final_path = self._get_artifact_path(artifact_id)
                if final_path.exists():
                    final_path.unlink()
                partial_path.rename(final_path)
                
                logger.info(f"Cached artifact {artifact_id} ({len(data)} bytes)")
                return True
                
            except OSError as e:
                logger.error(f"Failed to cache artifact {artifact_id}: {e}")
                return False
    
    def get(self, artifact_id: str, expected_digest: Optional[str] = None) -> Optional[bytes]:
        """Retrieve artifact from cache with optional digest validation.
        
        If expected_digest is provided, the cached artifact's digest is verified
        before returning. Mismatch raises DigestMismatchError.
        
        Args:
            artifact_id: Unique identifier for the artifact
            expected_digest: Expected SHA-256 digest for validation
        
        Returns:
            Artifact bytes if cache hit and valid, None if not found
        
        Raises:
            DigestMismatchError: If cached digest doesn't match expected
            CacheCorruptError: If cache entry is marked corrupt
        """
        artifact_path = self._get_artifact_path(artifact_id)
        
        with self._lock:
            metadata = self._metadata.get(artifact_id)
            
            if metadata is None:
                return None
            
            # Check if entry was marked corrupt
            if metadata.state == CacheState.CORRUPT:
                raise CacheCorruptError(f"Cache entry {artifact_id} marked corrupt")
            
            # Check if entry is being downloaded by another thread
            if artifact_id in self._downloading:
                # Could wait here, but for simplicity return None
                return None
            
            # Read the cached data
            try:
                with open(artifact_path, 'rb') as f:
                    data = f.read()
            except FileNotFoundError:
                logger.warning(f"Artifact file missing for {artifact_id}, evicting")
                self._evict_entry(artifact_id)
                return None
            
            # Validate digest if provided
            if expected_digest:
                if not self._verify_digest(data, expected_digest):
                    logger.error(f"Digest mismatch for {artifact_id}")
                    self._mark_corrupt(artifact_id)
                    raise DigestMismatchError(
                        f"Artifact {artifact_id} digest mismatch. "
                        f"Expected {expected_digest}, got {self._compute_digest(data)}"
                    )
                
                # Also validate against stored digest
                if not self._verify_digest(data, metadata.digest):
                    logger.error(f"Stored digest mismatch for {artifact_id}")
                    self._mark_corrupt(artifact_id)
                    raise DigestMismatchError(
                        f"Artifact {artifact_id} stored digest mismatch"
                    )
            elif expected_digest is None and metadata.digest:
                # Always verify against stored digest if available
                if not self._verify_digest(data, metadata.digest):
                    logger.error(f"Digest mismatch for {artifact_id} vs stored")
                    self._mark_corrupt(artifact_id)
                    raise DigestMismatchError(
                        f"Artifact {artifact_id} stored digest mismatch"
                    )
            
            # Update last_verified timestamp
            import time
            metadata.last_verified = time.time()
            self._save_metadata(metadata)
            
            return data
    
    def has_valid(self, artifact_id: str, digest: Optional[str] = None) -> bool:
        """Check if a valid cache entry exists for the artifact.
        
        Args:
            artifact_id: Unique identifier for the artifact
            digest: Optional digest to verify against
        
        Returns:
            True if valid cache entry exists and digest matches
        """
        artifact_path = self._get_artifact_path(artifact_id)
        
        with self._lock:
            metadata = self._metadata.get(artifact_id)
            
            if metadata is None:
                return False
            
            if metadata.state != CacheState.VALID:
                return False
            
            # If digest provided, verify it matches stored
            if digest and metadata.digest != digest:
                return False
            
            # Verify file exists
            return artifact_path.exists()
    
    def _mark_corrupt(self, artifact_id: str) -> None:
        """Mark a cache entry as corrupt and schedule eviction."""
        metadata = self._metadata.get(artifact_id)
        if metadata:
            metadata.state = CacheState.CORRUPT
            self._save_metadata(metadata)
            logger.warning(f"Marked {artifact_id} as corrupt")
    
    def _evict_entry(self, artifact_id: str) -> None:
        """Evict a cache entry completely (remove file and metadata)."""
        artifact_path = self._get_artifact_path(artifact_id)
        meta_path = self._get_metadata_path(artifact_id)
        partial_path = self._get_partial_path(artifact_id)
        
        # Remove files
        for path in [artifact_path, meta_path, partial_path]:
            path.unlink(missing_ok=True)
        
        # Remove from metadata
        if artifact_id in self._metadata:
            del self._metadata[artifact_id]
        
        logger.info(f"Evicted cache entry {artifact_id}")
    
    def evict(self, artifact_id: str) -> bool:
        """Evict a cache entry by ID.
        
        Args:
            artifact_id: Unique identifier for the artifact
        
        Returns:
            True if evicted, False if not found
        """
        with self._lock:
            if artifact_id not in self._metadata:
                return False
            self._evict_entry(artifact_id)
            return True
    
    def evict_corrupt(self) -> int:
        """Evict all entries marked as corrupt.
        
        Returns:
            Number of entries evicted
        """
        with self._lock:
            corrupt_ids = [
                aid for aid, meta in self._metadata.items()
                if meta.state == CacheState.CORRUPT
            ]
            for aid in corrupt_ids:
                self._evict_entry(aid)
            return len(corrupt_ids)
    
    def start_download(self, artifact_id: str) -> bool:
        """Mark that a download has started for an artifact.
        
        This prevents other threads from trying to get the same artifact
        while it's being downloaded.
        
        Args:
            artifact_id: Unique identifier for the artifact
        
        Returns:
            True if download started, False if already in progress
        """
        with self._lock:
            if artifact_id in self._downloading:
                return False
            self._downloading.add(artifact_id)
            return True
    
    def end_download(self, artifact_id: str) -> None:
        """Mark that a download has completed for an artifact.
        
        Args:
            artifact_id: Unique identifier for the artifact
        """
        with self._lock:
            self._downloading.discard(artifact_id)
    
    def get_cache_size(self) -> int:
        """Get total size of cached artifacts in bytes."""
        total = 0
        for artifact_path in self.cache_dir.glob("*"):
            if artifact_path.suffix != self.METADATA_SUFFIX and artifact_path.suffix != self.PARTIAL_SUFFIX:
                try:
                    total += artifact_path.stat().st_size
                except OSError:
                    pass
        return total
    
    def list_entries(self) -> Dict[str, CacheMetadata]:
        """List all cache entries with their metadata."""
        with self._lock:
            return dict(self._metadata)
    
    def clear(self) -> int:
        """Clear entire cache (remove all artifacts and metadata).
        
        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._metadata)
            for artifact_id in list(self._metadata.keys()):
                self._evict_entry(artifact_id)
            return count


# Global cache instance
_global_cache: Optional[ArtifactDownloadCache] = None


def get_download_cache(
    cache_dir: str = "/var/cache/orchestrator/artifacts",
    digest_algorithm: str = "sha256",
) -> ArtifactDownloadCache:
    """Get the global artifact download cache instance."""
    global _global_cache
    if _global_cache is None:
        _global_cache = ArtifactDownloadCache(
            cache_dir=cache_dir,
            digest_algorithm=digest_algorithm,
        )
    return _global_cache


def reset_download_cache() -> None:
    """Reset the global cache (for testing)."""
    global _global_cache
    if _global_cache:
        _global_cache.clear()
    _global_cache = None


# 2026-05-25T20:04:00 update - Initial implementation for issue #4321
# Add checksum validation to download cache for artifact consumers
