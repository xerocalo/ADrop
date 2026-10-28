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


def test_gui_analysis_and_error_recovery(tmp_path):
    root = tk.Tk()
    root.withdraw()
    app = OrganizerApp(root)
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
