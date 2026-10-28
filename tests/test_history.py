import json
from pathlib import Path
import logging
import pytest
from download_organizer.history import History, configure_logging
from download_organizer.organizer import preview, execute


def run(tmp_path, duplicates=False):
    root = tmp_path / "files"
    root.mkdir()
    (root / "a").write_bytes(b"same")
    if duplicates:
        (root / "b").write_bytes(b"same")
    store = History(tmp_path / "app")
    result = execute(preview(root, "modified", scope=tmp_path), confirmed=True,
                     quarantine=tmp_path / "q", history=store)
    assert not result.errors
    return root, store, store.list()[0]


def test_restore_restart_and_repeat(tmp_path):
    root, store, job = run(tmp_path, True)
    store = History(tmp_path / "app")
    job = store.list()[0]
    assert all(store.inspect(job, e) == "복원 가능" for e in job["entries"])
    store.restore(job, confirmed=True)
    assert (root / "a").read_bytes() == (root / "b").read_bytes() == b"same"
    assert all(e["status"] == "restored" for e in store.list()[0]["entries"])
    store.restore(job, confirmed=True)
    assert (root / "a").read_bytes() == b"same"


def test_conflict_and_partial_failure(tmp_path):
    root, store, job = run(tmp_path, True)
    conflict = Path(job["entries"][0]["source"])
    conflict.write_bytes(b"other")
    results = store.restore(job, confirmed=True)
    assert any("충돌" in r for r in results)
    assert "복원됨" in results
    assert conflict.read_bytes() == b"other"


def test_interrupted_job_recovery(tmp_path):
    root, store, job = run(tmp_path)
    job["state"] = "running"
    job["ended"] = None
    job["entries"][0]["status"] = "planned"
    store.save(job)
    recovered = History(tmp_path / "app").list()[0]
    assert recovered["state"] == "running"
    assert store.inspect(recovered, recovered["entries"][0]) == "복원 가능"
    store.restore(recovered, confirmed=True)
    assert (root / "a").exists()


def test_changed_or_missing_quarantine(tmp_path):
    root, store, job = run(tmp_path, True)
    entry = next(e for e in job["entries"] if e["kind"] == "quarantine")
    target = Path(entry["destination"])
    target.write_bytes(b"changed")
    assert "내용 변경" in store.inspect(job, entry)
    target.unlink()
    assert "복구 불가" in store.inspect(job, entry)


def test_corrupt_missing_manifest(tmp_path):
    store = History(tmp_path)
    assert store.list() == []
    store.base.mkdir()
    (store.base / "broken.json").write_text("{bad", encoding="utf-8")
    job = store.list()[0]
    assert job["state"] == "damaged"
    assert store.restore(job, confirmed=True) == []


def test_manifest_failure_before_plan_does_not_move(tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir()
    (root / "a").write_bytes(b"data")
    store = History(tmp_path / "app")
    def fail(job):
        raise OSError("disk full")
    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        execute(preview(root, "modified", scope=tmp_path), confirmed=True, history=store, quarantine=tmp_path / "q")
    assert list(root.iterdir()) == [root / "a"]


def test_confirmation_and_purge(tmp_path):
    root, store, job = run(tmp_path, True)
    index = next(i for i, e in enumerate(job["entries"]) if e["kind"] == "quarantine")
    target = Path(job["entries"][index]["destination"])
    store.restore(job, confirmed=False)
    store.purge(job, [index], confirmed=False)
    assert target.exists()
    store.purge(job, [index], confirmed=True)
    assert not target.exists()
    assert "복구 불가" in store.inspect(store.list()[0], store.list()[0]["entries"][index])


def test_log_rotation_and_failure_isolation(tmp_path):
    logger = configure_logging(tmp_path)
    handler = logger.handlers[-1]
    assert handler.maxBytes == 2 * 1024 * 1024 and handler.backupCount == 5
    try:
        for _ in range(8):
            logger.info("한글" * 250000)
        handler.flush()
        assert (tmp_path / "logs/organizer.log.1").exists()
        assert len(list((tmp_path / "logs").glob("organizer.log*"))) <= 6
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_actual_process_exit_between_move_and_manifest_commit(tmp_path):
    import subprocess
    import sys
    root = tmp_path / "files"
    root.mkdir()
    (root / "a").write_bytes(b"data")
    script = '''
import os, sys
from pathlib import Path
from download_organizer import organizer as o
from download_organizer.history import History
base = Path(sys.argv[1])
original = o.remove_source
def interrupt(path, protected):
    original(path, protected)
    os._exit(73)
o.remove_source = interrupt
o.execute(o.preview(base / 'files', 'modified', scope=base), confirmed=True,
          quarantine=base / 'q', history=History(base / 'app'))
'''
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=False)
    assert child.returncode == 73
    store = History(tmp_path / "app")
    job = store.list()[0]
    assert job["state"] == "running" and job["entries"][0]["status"] == "planned"
    assert store.inspect(job, job["entries"][0]) == "복원 가능"
    store.restore(job, confirmed=True)
    assert (root / "a").read_bytes() == b"data"
