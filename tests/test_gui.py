"""Windows CI에서 실제 Tk 창과 작업 스레드의 결과 전달을 검증한다."""
import os
import time
import tkinter as tk

import pytest
from download_organizer.gui import OrganizerApp

pytestmark = pytest.mark.skipif(
    os.name != "nt" and not os.environ.get("DISPLAY"),
    reason="GUI 통합 검증에 Windows 또는 X 디스플레이가 필요합니다.",
)


def wait_until_idle(root, app):
    deadline = time.monotonic() + 10
    while app.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert not app.busy, "백그라운드 분석이 완료되지 않았습니다."


def test_gui_analysis_and_error_recovery(tmp_path, monkeypatch):
    import download_organizer.gui as gui
    from download_organizer.organizer import preview
    monkeypatch.setattr(gui, "preview", lambda folder, basis, **kw: preview(folder, basis, scope=tmp_path.parent, **kw))
    root = tk.Tk()
    root.withdraw()
    app = OrganizerApp(root)
    app.basis.set("수정일")
    try:
        file = tmp_path / "한글 공백 #.txt"
        file.write_bytes(b"hello")
        app.path.set(str(tmp_path))
        app.start()
        wait_until_idle(root, app)
        assert len(app.table.get_children()) == 1
        assert "파일 1개" in app.status.get()
        assert file.read_bytes() == b"hello"
        app.path.set(str(tmp_path / "missing"))
        app.start()
        wait_until_idle(root, app)
        assert "오류 1개" in app.status.get()
        assert "오류:" in app.messages.get("1.0", "end")
        app.path.set(str(tmp_path))
        app.start()
        wait_until_idle(root, app)
        assert len(app.table.get_children()) == 1
    finally:
        app.close()


def test_gui_confirmation_cancel_has_no_side_effects(tmp_path, monkeypatch):
    import download_organizer.gui as gui
    from download_organizer.organizer import preview
    root = tk.Tk()
    root.withdraw()
    app = OrganizerApp(root)
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "a").write_bytes(b"same")
    (folder / "b").write_bytes(b"same")
    try:
        app.prepared = preview(folder, "modified", scope=tmp_path)
        asked = []
        def decline(*args, **kwargs):
            asked.append(args)
            return False
        monkeypatch.setattr(gui.messagebox, "askokcancel", decline)
        monkeypatch.setattr(gui, "execute", lambda *a, **kw: pytest.fail("취소 후 실행됨"))
        before = sorted(str(p) for p in tmp_path.rglob("*"))
        app.organize()
        assert asked
        assert not app.busy
        assert before == sorted(str(p) for p in tmp_path.rglob("*"))
    finally:
        app.close()


def test_gui_confirmed_execution(tmp_path, monkeypatch):
    import download_organizer.gui as gui
    from download_organizer.organizer import preview, execute
    root = tk.Tk()
    root.withdraw()
    app = OrganizerApp(root)
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "a").write_bytes(b"same")
    (folder / "b").write_bytes(b"same")
    try:
        app.prepared = preview(folder, "modified", scope=tmp_path)
        monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **kw: True)
        monkeypatch.setattr(gui, "execute", lambda prepared, **kw: execute(prepared, quarantine=tmp_path / "q", **kw))
        app.organize()
        wait_until_idle(root, app)
        assert "이동 1개" in app.status.get()
        assert "격리 1개" in app.status.get()
        assert "오류/건너뜀 0개" in app.status.get()
        assert app.prepared is None
        assert not (folder / "a").exists() and not (folder / "b").exists()
    finally:
        app.close()
