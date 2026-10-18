from __future__ import annotations

import os
from pathlib import Path
import uuid


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Not all filesystems expose directory fsync; the file itself is still fsynced.
        pass


def write_upload_staging_file(storage_root: Path, receipt_id: str, data: bytes) -> Path:
    """Create an exclusive, private, fsynced staging file inside the storage filesystem."""
    root = storage_root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = root / f".{receipt_id}.uploading"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(staging, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(root)
    except Exception:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return staging


def promote_upload_staging_file(staging_path: Path, final_path: Path, storage_root: Path) -> Path:
    """Atomically publish a staged file without overwriting an existing document."""
    root = storage_root.resolve()
    staging = staging_path.resolve(strict=True)
    final = final_path.resolve(strict=False)
    if not staging.is_relative_to(root) or not final.is_relative_to(root):
        raise ValueError("Upload paths must remain inside protected storage")
    if staging.is_symlink() or not staging.is_file():
        raise ValueError("Upload staging file is missing or unsafe")
    if final.exists() or final.is_symlink():
        raise FileExistsError("A document already exists at the generated storage path")
    # Hard-link creation is atomic and fails if the target exists. The staging and final
    # paths live on the same filesystem; unlinking the temporary name occurs only after
    # the final durable name has been created.
    os.link(staging, final, follow_symlinks=False)
    _fsync_directory(root)
    staging.unlink()
    _fsync_directory(root)
    return final


def safely_remove_storage_file(candidate: Path | str, storage_root: Path) -> bool:
    """Remove one stale file only if its resolved parent is inside the storage root."""
    root = storage_root.resolve()
    path = Path(candidate)
    if not path.is_absolute():
        path = root / path
    try:
        parent = path.parent.resolve(strict=True)
        if not parent.is_relative_to(root) or path.parent.is_symlink():
            return False
        if path.name in {"", ".", ".."}:
            return False
        if path.is_symlink():
            path.unlink()
        elif path.is_file():
            path.unlink()
        else:
            return False
        _fsync_directory(parent)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def new_storage_suffix() -> str:
    """Generate an opaque entropy suffix when an invoice ID is not yet available."""
    return uuid.uuid4().hex
