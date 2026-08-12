#!/usr/bin/env python3
"""Safe attachment-directory retention action."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import attune


def main(attachment_directory: str, attachment_directory_maximum_size: int = 50, attachment_days_to_keep: int = 7):
    root = Path(attachment_directory)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("attachment_directory must be an existing absolute directory")
    files = [path for path in root.iterdir() if path.is_file() and not path.is_symlink()]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=attachment_days_to_keep)).timestamp()
    deleted_files = deleted_bytes = 0
    for path in list(files):
        stat = path.stat()
        if stat.st_mtime < cutoff:
            size = stat.st_size
            path.unlink()
            deleted_files += 1
            deleted_bytes += size
            files.remove(path)
    maximum = attachment_directory_maximum_size * 1024 * 1024
    remaining = sum(path.stat().st_size for path in files)
    for path in sorted(files, key=lambda item: item.stat().st_size, reverse=True):
        if remaining <= maximum:
            break
        size = path.stat().st_size
        path.unlink()
        deleted_files += 1
        deleted_bytes += size
        remaining -= size
    return {"deleted_files": deleted_files, "deleted_bytes": deleted_bytes, "remaining_bytes": remaining}


if __name__ == "__main__":
    attune.run_action(main)
