from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
import json
import os

import pytest
from download_organizer import organizer as o


def setup_files(tmp_path, files):
    root = tmp_path / "다운로드 폴더"
    root.mkdir()
    for name, content in files.items():
        (root / name).write_bytes(content)
    return root


def prepare(root):
    return o.preview(root, "modified", scope=root.parent)


def test_same_size_different_content_and_unique_size_not_hashed(tmp_path, monkeypatch):
    root = setup_files(tmp_path, {"a": b"abc", "b": b"xyz", "c": b"longer"})
    seen = []
    original = o.digest_file
    def track(path, snapshot, cancel=None):
        seen.append(path.name)
        return original(path, snapshot, cancel)
    monkeypatch.setattr(o, "digest_file", track)
    result = prepare(root)
    assert not result.errors
    assert set(seen) == {"a", "b"}
    assert not result.keepers


def test_duplicate_keep_override_quarantine_record(tmp_path):
    root = setup_files(tmp_path, {"원본 파일.txt": b"same", "다른 이름.txt": b"same"})
    result = prepare(root)
    assert len(result.keepers) == 1
    keep = root / "다른 이름.txt"
    result.keep(keep)
    assert result.summary() == (2, 1, 4)
    assert list(root.iterdir()) and not (tmp_path / "quarantine").exists()
    # 보존본은 날짜 이동에서 제외해 격리된 파일의 보존 대상을 직접 확인한다.
    next(i for i in result.items if i.plan.source == keep).selected = False
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "quarantine")
    assert not outcome.errors
    assert len(outcome.quarantined) == 1
    assert keep.read_bytes() == b"same"
    quarantined = Path(outcome.quarantined[0])
    assert quarantined.read_bytes() == b"same"
    record = json.loads((quarantined.parent.parent / "original.json").read_text(encoding="utf-8"))
    assert record["original"] == str(root / "원본 파일.txt")
    assert not (root / "원본 파일.txt").exists()


def test_creation_order_default_keeper(tmp_path, monkeypatch):
    from dataclasses import replace
    root = setup_files(tmp_path, {"old": b"same", "new": b"same"})
    original = o.analyze
    def fake(*args, **kwargs):
        result = original(*args, **kwargs)
        result.plans = [replace(p, created=datetime(2020 if p.source.name == "old" else 2024, 1, 1)) for p in result.plans]
        return result
    monkeypatch.setattr(o, "analyze", fake)
    assert list(prepare(root).keepers.values()) == [root / "old"]


def test_hash_collision_still_byte_compared(tmp_path, monkeypatch):
    root = setup_files(tmp_path, {"a": b"abc", "b": b"xyz"})
    monkeypatch.setattr(o, "digest_file", lambda *args: "same-hash")
    assert not prepare(root).keepers


def test_filename_conflict_no_overwrite(tmp_path):
    root = setup_files(tmp_path, {"a": b"original"})
    result = prepare(root)
    target = result.items[0].plan.destination
    target.parent.mkdir()
    target.write_bytes(b"existing")
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "q")
    assert outcome.errors and not outcome.moved
    assert target.read_bytes() == b"existing"
    assert (root / "a").read_bytes() == b"original"


def test_unicode_long_path_and_existing_date_folder(tmp_path):
    root = tmp_path
    for _ in range(4):
        root = root / ("한글 공백 #" * 6)
    root.mkdir(parents=True)
    source = root / "긴 파일명 ! [문서].txt"
    source.write_bytes(b"data")
    result = o.preview(root, "modified", scope=tmp_path)
    target = result.items[0].plan.destination
    target.parent.mkdir()
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "q")
    assert not outcome.errors
    assert target.read_bytes() == b"data"
    assert not source.exists()


def test_read_permission_error(tmp_path, monkeypatch):
    root = setup_files(tmp_path, {"bad": b"abc", "good": b"xyz"})
    original = Path.open
    def denied(self, *args, **kwargs):
        if self == root / "bad":
            raise PermissionError("접근 거부")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    result = prepare(root)
    assert result.errors
    assert [i.plan.source.name for i in result.items] == ["good"]


def test_file_changed_after_preview(tmp_path):
    root = setup_files(tmp_path, {"a": b"original"})
    result = prepare(root)
    (root / "a").write_bytes(b"changed!")
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "q")
    assert outcome.errors and not outcome.moved
    assert (root / "a").read_bytes() == b"changed!"
    assert not result.items[0].plan.destination.exists()


def test_file_changed_during_operation(tmp_path, monkeypatch):
    root = setup_files(tmp_path, {"a": b"original"})
    result = prepare(root)
    original = o.digest_file
    calls = 0
    def change(path, snapshot, cancel=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            path_to_change = root / "a"
            if os.name == "nt":
                # 실제 Windows 잠금은 외부 쓰기를 거부한다.
                with pytest.raises(PermissionError):
                    path_to_change.write_bytes(b"modified")
                raise ValueError("변경 시도 감지 테스트")
            path_to_change.write_bytes(b"modified")
        return original(path, snapshot, cancel)
    monkeypatch.setattr(o, "digest_file", change)
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "q")
    assert outcome.errors and not outcome.moved
    assert (root / "a").exists()
    assert not result.items[0].plan.destination.exists()


def test_cancel_confirmation_changes_nothing(tmp_path):
    root = setup_files(tmp_path, {"a": b"same", "b": b"same"})
    result = prepare(root)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    outcome = o.execute(result, confirmed=False, quarantine=tmp_path / "q")
    assert not outcome.moved and not outcome.quarantined
    assert before == sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))


def test_keeper_changed_no_quarantine(tmp_path):
    root = setup_files(tmp_path, {"a": b"same", "b": b"same"})
    result = prepare(root)
    keeper = next(iter(result.keepers.values()))
    keeper.write_bytes(b"diff")
    for item in result.items:
        item.selected = item.plan.source != keeper
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "q")
    assert outcome.errors and not outcome.quarantined
    assert len(list(root.iterdir())) == 2


def test_scope_and_symlink_protection(tmp_path):
    root = setup_files(tmp_path, {"a": b"data"})
    assert o.preview(root, "modified", scope=root).errors
    assert o.preview(root, "modified", scope=tmp_path / "other").errors
    appdata = tmp_path / "AppData" / "temp"
    appdata.mkdir(parents=True)
    assert o.preview(appdata, "modified", scope=tmp_path).errors
    result = prepare(root)
    target = result.items[0].plan.destination.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("링크 권한 없음")
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "q")
    assert outcome.errors and (root / "a").exists()
    assert not list(outside.iterdir())


def test_large_file_chunked_hash_and_comparison(tmp_path):
    payload = b"a" * (o.CHUNK + 71)
    root = setup_files(tmp_path, {"a": payload, "b": payload})
    result = prepare(root)
    assert len(result.keepers) == 1
    assert result.summary() == (2, 1, len(payload))


def test_selection_excludes_operation(tmp_path):
    root = setup_files(tmp_path, {"a": b"data"})
    result = prepare(root)
    result.items[0].selected = False
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "q")
    assert not outcome.errors and not outcome.moved
    assert list(root.iterdir()) == [root / "a"]


def test_full_duplicate_group_moves_keeper_and_quarantines_rest(tmp_path):
    root = setup_files(tmp_path, {"a": b"same", "b": b"same", "original.json": b"same"})
    result = prepare(root)
    result.keep(root / "a")
    outcome = o.execute(result, confirmed=True, quarantine=tmp_path / "q")
    assert not outcome.errors
    assert len(outcome.moved) == 1 and len(outcome.quarantined) == 2
    assert all(Path(p).read_bytes() == b"same" for p in outcome.moved + outcome.quarantined)
    assert not any((root / name).exists() for name in ("a", "b", "original.json"))
