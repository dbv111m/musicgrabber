"""Background snapshots for filesystem-backed UI listings."""

import os
import threading
import time

from constants import AUDIO_EXTENSIONS, LIBRARY_SCAN_TTL_SECONDS, LIBRARY_SCAN_RETRY_SECONDS
from utils import iter_library_audio_files


class LibrarySnapshots:
    """Keep the last complete result while one worker refreshes each scope."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def get(self, key, loader):
        with self.lock:
            entry = self.entries.setdefault(key, {
                "value": None, "finished": 0, "running": False, "error": False,
            })
            ttl = LIBRARY_SCAN_RETRY_SECONDS if entry["error"] else LIBRARY_SCAN_TTL_SECONDS
            if not entry["running"] and (not entry["finished"] or time.monotonic() - entry["finished"] >= ttl):
                entry["running"] = True

                def refresh():
                    try:
                        value = loader()
                        with self.lock:
                            entry.update(value=value, error=False)
                    except Exception as exc:
                        print(f"Library listing scan failed: {exc}")
                        with self.lock:
                            entry["error"] = True
                    finally:
                        with self.lock:
                            entry.update(finished=time.monotonic(), running=False)

                threading.Thread(target=refresh, daemon=True).start()
            state = "refreshing" if entry["running"] else "error" if entry["error"] else "ready"
            return entry["value"], state


snapshots = LibrarySnapshots()


def scan_playlists(directory):
    if directory is None:
        return []
    def failed(exc):
        raise exc

    names = set()
    # A not-yet-created Playlists subdirectory is valid on a new installation.
    if not directory.exists():
        if not directory.parent.exists():
            raise FileNotFoundError(f"Library directory is unavailable: {directory.parent}")
        return []
    for _root, _dirs, files in os.walk(directory, onerror=failed):
        names.update(name[:-4] for name in files if name.endswith('.m3u'))
    return sorted(names, key=str.casefold)


def scan_storage(directories):
    storage_bytes = file_count = 0
    seen = set()
    for directory in directories:
        if not directory.exists() and directory.parent.exists():
            continue  # Optional library subdirectories need not exist yet.
        for path in iter_library_audio_files(directory, AUDIO_EXTENSIONS, strict=True):
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            if key not in seen:
                storage_bytes += stat.st_size
                file_count += 1
                seen.add(key)
    return {"storage_bytes": storage_bytes, "file_count": file_count}
