"""Tests for backup.py — archive round-trip, live-db snapshotting, exclusions,
restore safety (traversal guard, non-empty refusal, force move-aside), listing,
and CLI arg-parsing smoke. All offline (sqlite + tarfile in tmp_path)."""

from __future__ import annotations

import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from ai_dev_assistant import cli
from ai_dev_assistant.backup import create_backup, list_backups, restore_backup
from ai_dev_assistant.config import Settings


def _make_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


def _db_rows(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY v")]
    finally:
        conn.close()


@pytest.fixture
def data_dir(tmp_path):
    """A representative data dir: run store, memory dbs, json state, noise."""
    d = tmp_path / "data"
    _make_db(d / "runs.db", ["run-a", "run-b"])
    _make_db(d / "projects" / "p1" / "memory.db", ["mem-1"])
    _make_db(d / "global" / "memory.db", ["gmem-1"])
    (d / "projects.json").write_text('[{"slug": "p1"}]')
    (d / "settings.json").write_text('{"effort": "high"}')
    (d / "users.json").write_text('[{"name": "sam", "sha256": "ab"}]')
    (d / "vapid.json").write_text('{"private_key": "SECRET", "public_key": "PUB"}')
    (d / "benchmarks.jsonl").write_text('{"suite": "replay"}\n')
    (d / "projects" / "p1" / "knowledge_graph.json").write_text('{"nodes": []}')
    # Things a backup must skip:
    (d / "graph.lock").write_text("")
    (d / "backups").mkdir()
    (d / "backups" / "ada-backup-old.tar.gz").write_bytes(b"old")
    return d


@pytest.fixture
def settings(data_dir):
    return Settings(data_dir=data_dir)


# ---- create + round-trip ----
def test_backup_roundtrip_restores_identical_files(settings, data_dir, tmp_path):
    archive = create_backup(settings)
    assert archive.parent == data_dir / "backups"
    assert archive.name.startswith("ada-backup-") and archive.name.endswith(".tar.gz")

    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    for expected in ("runs.db", "projects/p1/memory.db", "global/memory.db",
                     "projects.json", "settings.json", "users.json", "vapid.json",
                     "benchmarks.jsonl", "projects/p1/knowledge_graph.json"):
        assert expected in names

    target = Settings(data_dir=tmp_path / "fresh")
    result = restore_backup(target, archive)
    rd = tmp_path / "fresh"
    assert result["restored_to"] == str(rd)
    assert result["moved_aside"] is None
    assert _db_rows(rd / "runs.db") == ["run-a", "run-b"]
    assert _db_rows(rd / "projects" / "p1" / "memory.db") == ["mem-1"]
    assert _db_rows(rd / "global" / "memory.db") == ["gmem-1"]
    for rel in ("projects.json", "settings.json", "users.json", "vapid.json",
                "benchmarks.jsonl"):
        assert (rd / rel).read_bytes() == (data_dir / rel).read_bytes()


def test_backup_snapshots_live_wal_db(settings, data_dir, tmp_path):
    """A held-open WAL connection with uncheckpointed writes must still yield a
    consistent, complete snapshot (sqlite3 backup API, not a file copy)."""
    conn = sqlite3.connect(data_dir / "runs.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO t (v) VALUES ('run-live')")
    conn.commit()  # sits in runs.db-wal until a checkpoint
    try:
        assert (data_dir / "runs.db-wal").exists()
        archive = create_backup(settings)
    finally:
        conn.close()
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "runs.db" in names and "runs.db-wal" not in names
    restore_backup(Settings(data_dir=tmp_path / "fresh"), archive)
    assert "run-live" in _db_rows(tmp_path / "fresh" / "runs.db")


def test_backup_excludes_backups_locks_and_sidecars(settings, data_dir):
    (data_dir / "runs.db-shm").write_bytes(b"x")
    archive = create_backup(settings)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert not any(n.startswith("backups") for n in names)
    assert "graph.lock" not in names
    assert "runs.db-shm" not in names
    # Sensitive-but-included by design: the user's own restore needs these.
    assert "users.json" in names and "vapid.json" in names


def test_backup_custom_out_dir(settings, tmp_path):
    out = tmp_path / "elsewhere"
    archive = create_backup(settings, out_dir=out)
    assert archive.parent == out and archive.exists()
    with tarfile.open(archive) as tar:
        assert "runs.db" in tar.getnames()


# ---- restore safety ----
def test_restore_rejects_traversal_archive(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        payload = b"pwned"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    target = Settings(data_dir=tmp_path / "victim")
    with pytest.raises(ValueError, match="unsafe path"):
        restore_backup(target, evil)
    assert not (tmp_path / "escaped.txt").exists()


def test_restore_refuses_nonempty_without_force(settings, data_dir, tmp_path):
    archive = create_backup(settings, out_dir=tmp_path / "out")
    with pytest.raises(ValueError, match="not empty"):
        restore_backup(settings, archive)
    assert (data_dir / "projects.json").exists()  # untouched


def test_restore_force_moves_data_dir_aside(settings, data_dir, tmp_path):
    archive = create_backup(settings, out_dir=tmp_path / "out")
    (data_dir / "projects.json").write_text('[{"slug": "changed-after-backup"}]')
    result = restore_backup(settings, archive, force=True)
    moved = result["moved_aside"]
    assert moved and ".pre-restore-" in moved
    # Nothing deleted: the pre-restore copy keeps the post-backup edit…
    assert "changed-after-backup" in (Path(moved) / "projects.json").read_text()
    # …while the data dir holds the archived state again.
    assert "p1" in (data_dir / "projects.json").read_text()
    assert _db_rows(data_dir / "runs.db") == ["run-a", "run-b"]
    assert result["members"] > 0


def test_restore_force_handles_archive_inside_data_dir(settings, data_dir):
    """Default backup location is <data_dir>/backups — a forced in-place
    restore moves the archive aside with the data dir and must still work."""
    archive = create_backup(settings)
    assert archive.is_relative_to(data_dir)
    result = restore_backup(settings, archive, force=True)
    assert result["moved_aside"] is not None
    assert _db_rows(data_dir / "runs.db") == ["run-a", "run-b"]


def test_restore_missing_archive_errors(settings, tmp_path):
    with pytest.raises(ValueError, match="no such archive"):
        restore_backup(settings, tmp_path / "nope.tar.gz")


# ---- listing ----
def test_list_backups_newest_first(settings, data_dir):
    b = data_dir / "backups"
    (b / "ada-backup-20250101T000000Z.tar.gz").write_bytes(b"a" * 10)
    (b / "ada-backup-20260101T000000Z.tar.gz").write_bytes(b"b" * 20)
    rows = list_backups(settings)
    names = [r["path"].name for r in rows]
    assert names.index("ada-backup-20260101T000000Z.tar.gz") < names.index(
        "ada-backup-20250101T000000Z.tar.gz")
    newest = rows[names.index("ada-backup-20260101T000000Z.tar.gz")]
    assert newest["size"] == 20 and newest["created"].startswith("2026-01-01")


# ---- CLI ----
def test_cli_backup_parsing():
    p = cli._build_parser()
    a = p.parse_args(["backup", "create", "--out", "/x/y"])
    assert (a.cmd, a.bcmd, a.out) == ("backup", "create", "/x/y")
    a = p.parse_args(["backup", "list"])
    assert a.bcmd == "list"
    a = p.parse_args(["backup", "restore", "b.tar.gz", "--force"])
    assert (a.bcmd, a.archive, a.force) == ("restore", "b.tar.gz", True)


def test_cli_backup_create_list_restore_flow(data_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ADA_DATA_DIR", str(data_dir))
    assert cli.main(["backup", "create"]) == 0
    out = capsys.readouterr().out
    assert "Backup written:" in out and "NOT included" in out
    assert cli.main(["backup", "list"]) == 0
    assert "ada-backup-" in capsys.readouterr().out
    # Restore without --force into the same (non-empty) data dir refuses (exit 2)
    archive = next((data_dir / "backups").glob("ada-backup-*T*.tar.gz"))
    assert cli.main(["backup", "restore", str(archive)]) == 2
    err = capsys.readouterr()
    assert "stop the server" in err.out and "not empty" in err.err
    # A fresh data dir restores cleanly
    monkeypatch.setenv("ADA_DATA_DIR", str(tmp_path / "fresh"))
    assert cli.main(["backup", "restore", str(archive)]) == 0
    assert "Restored" in capsys.readouterr().out
    assert (tmp_path / "fresh" / "runs.db").exists()
