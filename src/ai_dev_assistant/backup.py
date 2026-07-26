"""Backup & restore for the ADA data directory.

Scope: the DATA DIR ONLY (`settings.data_dir`) — the run store (`runs.db`),
per-project and global memory stores, knowledge graphs, `projects.json`,
settings overlay, `users.json`, `vapid.json`, benchmark history, and any other
state files that live there. SQLite databases are snapshotted with the sqlite3
backup API first, so archiving a live server can never capture a torn write
(WAL/SHM sidecars are therefore not archived — the snapshot is self-contained).

NOT included, by design:
- workspace checkouts (`settings.workspace_dir`) — materialized repos and git
  worktrees are re-creatable from their origins;
- docs output (`settings.docs_dir`) — generated artifacts, git-backed in
  practice;
- `<data_dir>/backups/` itself, and `*.lock` files.

`users.json` (token hashes) and `vapid.json` (push keys, including the private
key) ARE included: a restore should bring back the user's own auth and push
setup. Treat archives as sensitive.

Restore is conservative: it refuses a non-empty data dir unless forced, and
with ``force=True`` it moves the current data dir aside to
``<data_dir>.pre-restore-<ts>`` rather than deleting anything. Extraction
rejects archive members that would escape the target directory.
"""

from __future__ import annotations

import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings

BACKUP_PREFIX = "ada-backup-"
_TS_FORMAT = "%Y%m%dT%H%M%SZ"

# Sidecars the sqlite snapshot makes redundant (and that must never be paired
# with a *different* generation of the main db file).
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FORMAT)


def _snapshot_sqlite(src: Path, dst: Path) -> None:
    """Copy a (possibly live) sqlite db into ``dst`` via the backup API.

    Unlike a file copy, this yields a consistent snapshot even while another
    process holds the database open in WAL mode.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _is_db(path: Path) -> bool:
    return path.suffix == ".db"


def _is_db_sidecar(name: str) -> bool:
    return any(name.endswith(f".db{suf}") for suf in _DB_SIDECAR_SUFFIXES)


def _excluded(rel: Path, backups_rel: Path | None) -> bool:
    """True for members that never belong in an archive: the backups dir
    itself, lock files, and sqlite WAL/SHM/journal sidecars."""
    if backups_rel is not None and (rel == backups_rel or backups_rel in rel.parents):
        return True
    if rel.name.endswith(".lock"):
        return True
    if _is_db_sidecar(rel.name):
        return True
    return False


def create_backup(settings: Settings, out_dir: Path | str | None = None) -> Path:
    """Archive the data dir into ``ada-backup-<UTC ts>.tar.gz`` and return its path.

    Default destination: ``<data_dir>/backups/`` (always excluded from the
    archive itself, as is any custom ``out_dir`` that happens to live inside
    the data dir).
    """
    data_dir = Path(settings.data_dir)
    if not data_dir.is_dir():
        raise ValueError(f"data dir does not exist: {data_dir}")
    dest = Path(out_dir) if out_dir else data_dir / "backups"
    dest.mkdir(parents=True, exist_ok=True)

    archive = dest / f"{BACKUP_PREFIX}{_utc_ts()}.tar.gz"
    seq = 1
    while archive.exists():  # same-second backups: bump a suffix, never overwrite
        archive = dest / f"{BACKUP_PREFIX}{_utc_ts()}-{seq}.tar.gz"
        seq += 1

    # Exclude the destination subtree when it lives inside the data dir
    # (containment checked on resolved paths; the walk stays unresolved).
    backups_rel: Path | None = None
    rdest, rdata = dest.resolve(), data_dir.resolve()
    if rdest != rdata and rdest.is_relative_to(rdata):
        backups_rel = rdest.relative_to(rdata)

    tmp = Path(tempfile.mkdtemp(prefix="ada-backup-", dir=dest))
    try:
        staged = tmp / archive.name
        with tarfile.open(staged, "w:gz") as tar:
            for path in sorted(data_dir.rglob("*")):
                rel = path.relative_to(data_dir)
                if _excluded(rel, backups_rel) or tmp in path.parents:
                    continue
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    continue  # never archive symlinks/sockets/etc.
                if path.is_dir():
                    tar.add(path, arcname=str(rel), recursive=False)
                elif _is_db(path):
                    snap = tmp / "db" / rel
                    _snapshot_sqlite(path, snap)
                    tar.add(snap, arcname=str(rel))
                else:
                    tar.add(path, arcname=str(rel))
        staged.replace(archive)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return archive


def list_backups(settings: Settings) -> list[dict]:
    """Backups in ``<data_dir>/backups/``, newest first:
    ``[{path, size, created}]`` (``created`` is ISO-8601 UTC)."""
    dest = Path(settings.data_dir) / "backups"
    out = []
    for path in dest.glob(f"{BACKUP_PREFIX}*.tar.gz"):
        stem = path.name[len(BACKUP_PREFIX):-len(".tar.gz")]
        try:  # timestamp from the name (authoritative); mtime as fallback
            created = datetime.strptime(stem.split("-")[0], _TS_FORMAT).replace(
                tzinfo=timezone.utc)
        except ValueError:
            created = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        out.append({"path": path, "size": path.stat().st_size,
                    "created": created.isoformat()})
    out.sort(key=lambda b: (b["created"], str(b["path"])), reverse=True)
    return out


def _check_members(tar: tarfile.TarFile, target: Path) -> list[tarfile.TarInfo]:
    """Traversal guard: every member must extract strictly inside ``target``.

    Rejects absolute names, ``..`` segments, and link members whose target
    escapes — a crafted archive fails loudly before anything is written.
    """
    resolved_target = target.resolve()
    members = []
    for m in tar.getmembers():
        name = Path(m.name)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError(f"unsafe path in archive: {m.name!r}")
        dest = (resolved_target / name).resolve()
        if dest != resolved_target and not dest.is_relative_to(resolved_target):
            raise ValueError(f"archive member escapes target dir: {m.name!r}")
        if m.issym() or m.islnk():
            raise ValueError(f"link member not allowed in backup archive: {m.name!r}")
        if not (m.isfile() or m.isdir()):
            continue  # devices/fifos: skip silently, never extract
        members.append(m)
    return members


def restore_backup(settings: Settings, archive_path: Path | str, *,
                   force: bool = False) -> dict:
    """Restore an archive into ``settings.data_dir``.

    - Refuses when the data dir is non-empty, unless ``force=True``.
    - With force, the current data dir is MOVED ASIDE to
      ``<data_dir>.pre-restore-<ts>`` (never deleted) before extracting.
    - Extraction runs behind a path-traversal guard.

    Returns ``{"restored_to", "archive", "members", "moved_aside"}``.
    """
    archive = Path(archive_path)
    if not archive.is_file():
        raise ValueError(f"no such archive: {archive}")
    data_dir = Path(settings.data_dir)

    moved_aside: Path | None = None
    if data_dir.exists() and any(data_dir.iterdir()):
        if not force:
            raise ValueError(
                f"data dir {data_dir} is not empty — pass force=True (CLI: --force) "
                "to move it aside and restore over it")
        moved_aside = data_dir.with_name(f"{data_dir.name}.pre-restore-{_utc_ts()}")
        seq = 1
        while moved_aside.exists():
            moved_aside = data_dir.with_name(
                f"{data_dir.name}.pre-restore-{_utc_ts()}-{seq}")
            seq += 1
        rarchive, rdata = archive.resolve(), data_dir.resolve()
        data_dir.replace(moved_aside)
        if rarchive.is_relative_to(rdata):
            # The archive lived inside the data dir (e.g. <data_dir>/backups/…)
            # and moved with it — keep restoring from its new home.
            archive = moved_aside / rarchive.relative_to(rdata)

    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = _check_members(tar, data_dir)
        tar.extractall(data_dir, members=members, filter="data")

    return {
        "restored_to": str(data_dir),
        "archive": str(archive),
        "members": sum(1 for m in members if m.isfile()),
        "moved_aside": str(moved_aside) if moved_aside else None,
    }
