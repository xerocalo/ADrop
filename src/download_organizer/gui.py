"""작업 스레드는 큐에만 기록하고 모든 Tk 호출은 메인 스레드에서 처리한다."""
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic
import tkinter as tk
from tkinter import filedialog, ttk

from .analyzer import AnalysisResult, Progress, analyze
from .paths import default_downloads


def human_size(value: int) -> str:
    return f"{value:,} bytes ({value / 1024 ** 2:,.2f} MiB)"


class OrganizerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Download Organizer — 읽기 전용 분석")
        root.geometry("1200x720")
        self.events: Queue = Queue()
        self.cancel = Event()
        self.busy = False
        self.closed = False
        self.path = tk.StringVar(value=str(default_downloads()))
        self.basis = tk.StringVar(value="수정일")
        self.status = tk.StringVar(value="파일을 이동하거나 삭제하지 않습니다. 폴더를 선택하고 분석하세요.")
        controls = ttk.Frame(root, padding=10)
        controls.pack(fill="x")
        ttk.Label(controls, text="분석 폴더").pack(side="left")
        self.path_entry = ttk.Entry(controls, textvariable=self.path)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.choose_button = ttk.Button(controls, text="폴더 선택", command=self.choose)
        self.choose_button.pack(side="left")
        self.date_combo = ttk.Combobox(controls, textvariable=self.basis, values=("수정일", "생성일"), state="readonly", width=8)
        self.date_combo.pack(side="left", padx=8)
        self.start_button = ttk.Button(controls, text="읽기 전용 분석", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(controls, text="취소", command=self.cancel.set, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        ttk.Label(root, textvariable=self.status, padding=10, wraplength=1150).pack(fill="x")
        self.bar = ttk.Progressbar(root, maximum=100)
        self.bar.pack(fill="x", padx=10)
        frame = ttk.Frame(root, padding=10)
        frame.pack(fill="both", expand=True)
        columns = ("name", "size", "created", "modified", "source", "destination", "warning")
        self.table = ttk.Treeview(frame, columns=columns, show="headings")
        for key, label, width in zip(columns,
                ("파일명", "용량(bytes)", "생성일", "수정일", "이동 전 경로", "이동 후 예상 경로", "주의"),
                (180, 110, 150, 150, 360, 420, 240)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, stretch=False)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(root, text="오류 및 제외 내역").pack(anchor="w", padx=10)
        self.messages = tk.Text(root, height=7, state="disabled", wrap="word")
        self.messages.pack(fill="x", padx=10, pady=(0, 10))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(75, self.poll)

    def choose(self):
        selected = filedialog.askdirectory(parent=self.root, title="분석 폴더 선택")
        if selected:
            self.path.set(selected)

    def set_busy(self, value: bool):
        self.busy = value
        for widget in (self.path_entry, self.choose_button, self.start_button):
            widget.configure(state="disabled" if value else "normal")
        self.date_combo.configure(state="disabled" if value else "readonly")
        self.cancel_button.configure(state="normal" if value else "disabled")

    def start(self):
        if self.busy:
            return
        self.set_busy(True)
        self.cancel.clear()
        self.table.delete(*self.table.get_children())
        self.log("")
        self.bar.configure(value=0, mode="indeterminate")
        self.bar.start(20)
        self.status.set("폴더 항목을 확인하고 있습니다…")
        folder = Path(self.path.get())
        basis = "created" if self.basis.get() == "생성일" else "modified"
        Thread(target=self.work, args=(folder, basis), daemon=True).start()

    def work(self, folder, basis):
        last = 0.0

        def update(event: Progress):
            nonlocal last
            now = monotonic()
            if now - last >= 0.1 or event.processed == event.total:
                self.events.put(("progress", event))
                last = now
        try:
            result = analyze(folder, basis, progress=update, cancel=self.cancel)
        except Exception as exc:
            result = AnalysisResult(errors=[f"분석 오류: {exc}"])
        self.events.put(("result", result))

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, event = self.events.get_nowait()
                if kind == "progress":
                    self.bar.stop()
                    self.bar.configure(mode="determinate", value=event.processed / max(event.total, 1) * 100)
                    self.status.set(f"항목 {event.processed:,}/{event.total:,} · 분석 파일 {event.files:,}개 · {human_size(event.bytes)}")
                else:
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                    self.render(event, 0)
        except Empty:
            pass
        self.root.after(75, self.poll)

    def render(self, result, offset):
        if self.closed:
            return
        def date(value: datetime | None):
            return value.strftime("%Y-%m-%d %H:%M:%S") if value else "제공되지 않음"
        for plan in result.plans[offset:offset + 150]:
            self.table.insert("", "end", values=(plan.source.name, f"{plan.size:,}", date(plan.created), date(plan.modified), str(plan.source), str(plan.destination), plan.warning))
        offset += 150
        if offset < len(result.plans):
            self.status.set(f"분석 결과 표시 중… {min(offset, len(result.plans)):,}/{len(result.plans):,}")
            self.root.after(1, self.render, result, offset)
            return
        self.bar.configure(value=0 if result.cancelled or (result.errors and not result.plans) else 100)
        self.status.set(f"{'취소됨(부분 결과)' if result.cancelled else '분석 종료'} · 파일 {len(result.plans):,}개 · {human_size(result.total_bytes)} · 제외 {len(result.skipped):,}개 · 오류 {len(result.errors):,}개 · 실제 변경 없음")
        self.log("\n".join([*(f"오류: {e}" for e in result.errors), *(f"제외: {s}" for s in result.skipped)]))
        self.set_busy(False)

    def log(self, text):
        self.messages.configure(state="normal")
        self.messages.delete("1.0", "end")
        self.messages.insert("1.0", text)
        self.messages.configure(state="disabled")

    def close(self):
        self.closed = True
        self.cancel.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    OrganizerApp(root)
    root.mainloop()
