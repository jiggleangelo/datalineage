import json
import os
import threading
from pathlib import Path


class FileVault:
    """Append-only contact vault.

    Records are retained until the configured vault file is removed or rotated by
    the operator. Deployments must establish and document their own retention and
    deletion schedule. The storage is permission-restricted but not encrypted;
    use an encrypted filesystem or a managed secret store where required.
    """

    def __init__(self, path, max_bytes=10 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()
        parent = self.path.parent
        if parent in {Path("."), Path(parent.anchor)}:
            raise ValueError("vault must be stored in a dedicated subdirectory")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)

    def put(self, kind, record):
        if not isinstance(kind, str) or not isinstance(record, dict):
            raise ValueError("invalid vault record")
        line = (json.dumps({"kind": kind, **record}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > 4096:
            raise OverflowError("vault record too large")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        with self._lock:
            fd = os.open(self.path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                if os.fstat(fd).st_size + len(line) > self.max_bytes:
                    raise OverflowError("vault size limit reached")
                view = memoryview(line)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("vault write failed")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)


class MemoryVault:
    def __init__(self):
        self._records = []
        self._lock = threading.Lock()

    def put(self, kind, record):
        with self._lock:
            self._records.append((kind, dict(record)))

    def count(self, kind):
        with self._lock:
            return sum(saved_kind == kind for saved_kind, _ in self._records)
