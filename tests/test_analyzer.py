from datetime import datetime
import os
from pathlib import Path
from threading import Event

import pytest
from download_organizer.analyzer import analyze


def test_plan_unicode_metadata_and_read_only(tmp_path):
    root = tmp_path / "한글 다운로드 # [자료]"
    root.mkdir()
    file = root / "공백 & 특수문자 문서.txt"
    file.write_bytes(b"hello")
    timestamp = datetime(2025, 3, 7, 12, 30).timestamp()
    os.utime(file, (timestamp, timestamp))
    before = file.stat()
    events = []
    result = analyze(root, progress=events.append)
    assert not result.errors
    assert len(result.plans) == 1
    plan = result.plans[0]
    assert plan.source == file
    assert plan.destination == root / "25.03.07" / file.name
    assert plan.size == result.total_bytes == 5
    assert plan.modified == datetime.fromtimestamp(timestamp)
    assert file.read_bytes() == b"hello"
    after = file.stat()
    assert (before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    assert list(root.iterdir()) == [file]
    assert events[-1].processed == events[-1].total == 1
    assert events[-1].files == 1
    assert events[-1].bytes == 5


@pytest.mark.parametrize("suffix", [".crdownload", ".part", ".partial", ".download", ".tmp", ".TEMP"])
def test_partial_files_excluded(tmp_path, suffix):
    (tmp_path / ("파일" + suffix)).write_bytes(b"partial")
    result = analyze(tmp_path)
    assert not result.plans and len(result.skipped) == 1


def test_subfolders_and_lock_files_excluded(tmp_path):
    sub = tmp_path / "folder"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested")
    (tmp_path / "~$document.docx").touch()
    result = analyze(tmp_path)
    assert not result.plans
    assert len(result.skipped) == 2


def test_date_folder_and_descendant_rejected(tmp_path):
    root = tmp_path / "25.03.07"
    nested = root / "nested"
    nested.mkdir(parents=True)
    for path in (root, nested):
        assert analyze(path).errors


def test_missing_folder_empty_folder_invalid_basis(tmp_path):
    assert analyze(tmp_path / "missing").errors
    assert analyze(tmp_path, "bad").errors
    events = []
    result = analyze(tmp_path, progress=events.append)
    assert not result.errors and not result.plans
    assert events[-1].total == 0


def test_collision_warns_without_changes(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("original")
    target = tmp_path / datetime.fromtimestamp(file.stat().st_mtime).strftime("%y.%m.%d")
    target.mkdir()
    (target / file.name).write_text("existing")
    result = analyze(tmp_path)
    assert result.plans[0].warning
    assert file.read_text() == "original"
    assert (target / file.name).read_text() == "existing"


def test_cancel(tmp_path):
    (tmp_path / "a").touch()
    cancel = Event()
    cancel.set()
    result = analyze(tmp_path, cancel=cancel)
    assert result.cancelled and not result.plans


def test_symlinks_excluded_and_root_rejected(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    file = target / "a.txt"
    file.touch()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
        (tmp_path / "file-link").symlink_to(file)
    except OSError:
        pytest.skip("심볼릭 링크 생성 권한 없음")
    assert not analyze(tmp_path).plans
    assert analyze(link).errors


def test_stat_failure_continues(tmp_path, monkeypatch):
    (tmp_path / "bad.txt").touch()
    (tmp_path / "good.txt").touch()
    original = os.DirEntry.stat
    def failing(self, **kwargs):
        if self.name == "bad.txt":
            raise PermissionError("접근 거부")
        return original(self, **kwargs)
    monkeypatch.setattr(os.DirEntry, "stat", failing)
    result = analyze(tmp_path)
    assert len(result.errors) == 1
    assert [p.source.name for p in result.plans] == ["good.txt"]


def test_created_date_semantics(tmp_path):
    file = tmp_path / "a"
    file.touch()
    info = file.stat()
    result = analyze(tmp_path, "created")
    if hasattr(info, "st_birthtime") or os.name == "nt":
        assert not result.errors
        assert result.plans[0].created is not None
        assert result.plans[0].destination.parent.name == result.plans[0].created.strftime("%y.%m.%d")
    else:
        assert not result.plans and result.errors
