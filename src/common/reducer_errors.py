"""Reducer Error Persistence — Event processing diagnostics."""

import json
import time
import uuid
from typing import Any, Dict, List, Optional
from pathlib import Path


class ReducerErrorRecord:
    """Single reducer error record."""
    
    def __init__(
        self,
        reducer_id: str,
        error_type: str,
        error_message: str,
        context: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ):
        self.id = str(uuid.uuid4())
        self.reducer_id = reducer_id
        self.error_type = error_type
        self.error_message = error_message
        self.context = context or {}
        self.task_id = task_id
        self.agent_id = agent_id
        self.timestamp = time.time()
        self.persisted_at = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "reducer_id": self.reducer_id,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "context": self.context,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "persisted_at": self.persisted_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReducerErrorRecord":
        record = cls(
            reducer_id=data["reducer_id"],
            error_type=data["error_type"],
            error_message=data["error_message"],
            context=data.get("context"),
            task_id=data.get("task_id"),
            agent_id=data.get("agent_id"),
        )
        record.id = data.get("id", record.id)
        record.timestamp = data.get("timestamp", record.timestamp)
        record.persisted_at = data.get("persisted_at")
        return record


class ReducerErrorStore:
    """Persistent storage for reducer errors."""
    
    def __init__(self, storage_path: str = "/tmp/reducer_errors"):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self._errors: List[ReducerErrorRecord] = []
        self._index: Dict[str, List[int]] = {}  # reducer_id -> list of indices
        self._load_existing()

    def _load_existing(self) -> None:
        """Load existing error records from storage."""
        error_file = self.storage_path / "errors.json"
        if error_file.exists():
            try:
                with open(error_file, "r") as f:
                    data = json.load(f)
                    for item in data:
                        record = ReducerErrorRecord.from_dict(item)
                        self._errors.append(record)
                        if record.reducer_id not in self._index:
                            self._index[record.reducer_id] = []
                        self._index[record.reducer_id].append(len(self._errors) - 1)
            except (json.JSONDecodeError, KeyError):
                pass

    def _persist(self) -> None:
        """Write errors to persistent storage."""
        error_file = self.storage_path / "errors.json"
        data = [record.to_dict() for record in self._errors]
        with open(error_file, "w") as f:
            json.dump(data, f)

    def record(
        self,
        reducer_id: str,
        error_type: str,
        error_message: str,
        context: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> ReducerErrorRecord:
        """Record a reducer error."""
        record = ReducerErrorRecord(
            reducer_id=reducer_id,
            error_type=error_type,
            error_message=error_message,
            context=context,
            task_id=task_id,
            agent_id=agent_id,
        )
        record.persisted_at = time.time()
        
        if reducer_id not in self._index:
            self._index[reducer_id] = []
        self._index[reducer_id].append(len(self._errors))
        self._errors.append(record)
        
        self._persist()
        return record

    def get_errors(
        self,
        reducer_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ReducerErrorRecord]:
        """Get reducer errors, optionally filtered by reducer_id."""
        if reducer_id:
            indices = self._index.get(reducer_id, [])
            errors = [self._errors[i] for i in indices[offset:offset + limit]]
        else:
            errors = self._errors[offset:offset + limit]
        return errors

    def get_error_by_id(self, error_id: str) -> Optional[ReducerErrorRecord]:
        """Get a specific error by ID."""
        for record in self._errors:
            if record.id == error_id:
                return record
        return None

    def clear_errors(self, before_timestamp: Optional[float] = None) -> int:
        """Clear errors, optionally before a timestamp. Returns count of cleared errors."""
        if before_timestamp is None:
            count = len(self._errors)
            self._errors.clear()
            self._index.clear()
            self._persist()
            return count
        
        original_count = len(self._errors)
        self._errors = [r for r in self._errors if r.timestamp >= before_timestamp]
        
        # Rebuild index
        self._index.clear()
        for i, record in enumerate(self._errors):
            if record.reducer_id not in self._index:
                self._index[record.reducer_id] = []
            self._index[record.reducer_id].append(i)
        
        cleared = original_count - len(self._errors)
        if cleared > 0:
            self._persist()
        return cleared

    def count(self, reducer_id: Optional[str] = None) -> int:
        """Count errors, optionally filtered by reducer_id."""
        if reducer_id:
            return len(self._index.get(reducer_id, []))
        return len(self._errors)


# Global error store instance
_error_store: Optional[ReducerErrorStore] = None


def get_error_store(storage_path: str = "/tmp/reducer_errors") -> ReducerErrorStore:
    """Get or create the global error store instance."""
    global _error_store
    if _error_store is None:
        _error_store = ReducerErrorStore(storage_path)
    return _error_store
