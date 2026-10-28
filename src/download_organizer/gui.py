"""작업 스레드는 큐에만 기록하고 모든 Tk 호출은 메인 스레드에서 처리한다."""
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

from .analyzer import AnalysisResult, Progress
from .paths import default_downloads
from .organizer import preview, execute, quarantine_directory
from .history import History, configure_logging


def human_size(value: int) -> str:
    return f"{value:,} bytes ({value / 1024 ** 2:,.2f} MiB)"


class OrganizerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Download Organizer — 미리보기 및 정리")
        root.geometry("1200x720")
        self.events: Queue = Queue()
        self.cancel = Event()
        self.busy = False
        self.closed = False
        self.prepared = None
        self.row_items = {}
        self.path = tk.StringVar(value=str(default_downloads()))
        self.basis = tk.StringVar(value="생성일")
        self.status = tk.StringVar(value="분석은 읽기 전용이며 정리는 최종 확인 후 실행됩니다. 폴더를 선택하세요.")
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
        actions = ttk.Frame(root, padding=6)
        actions.pack(fill="x")
        ttk.Button(actions, text="선택 행 처리 여부 전환", command=self.toggle).pack(side="left")
        ttk.Button(actions, text="선택 파일을 중복 보존본으로", command=self.keep_selected).pack(side="left", padx=8)
        self.organize_button = ttk.Button(actions, text="선택 파일 정리…", command=self.organize, state="disabled")
        self.organize_button.pack(side="left")
        ttk.Button(actions, text="작업 이력 / 복원 / 격리 관리", command=self.history_window).pack(side="left", padx=8)
        ttk.Label(root, textvariable=self.status, padding=10, wraplength=1150).pack(fill="x")
        self.bar = ttk.Progressbar(root, maximum=100)
        self.bar.pack(fill="x", padx=10)
        frame = ttk.Frame(root, padding=10)
        frame.pack(fill="both", expand=True)
        columns = ("name", "size", "created", "modified", "source", "destination", "warning")
        self.table = ttk.Treeview(frame, columns=columns, show="headings")
        for key, label, width in zip(columns,
                ("파일명", "용량(bytes)", "생성일", "수정일", "이동 전 경로", "이동 후 예상 경로", "처리 / 중복 / 주의"),
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
        self.organize_button.configure(state="disabled" if value or self.prepared is None else "normal")
        for widget in (self.path_entry, self.choose_button, self.start_button):
            widget.configure(state="disabled" if value else "normal")
        self.date_combo.configure(state="disabled" if value else "readonly")
        self.cancel_button.configure(state="normal" if value else "disabled")

    def start(self):
        if self.busy:
            return
        self.prepared = None
        self.row_items.clear()
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
            result = preview(folder, basis, progress=update, cancel=self.cancel)
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
                elif kind == "done":
                    self.bar.stop()
                    self.status.set(f"작업 완료 · 이동 {len(event.moved)}개 · 격리 {len(event.quarantined)}개 · 오류/건너뜀 {len(event.errors)}개. 다시 분석하세요.")
                    self.log("\n".join(event.errors))
                    self.prepared = None
                    self.set_busy(False)
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
            row = self.table.insert("", "end", values=(plan.source.name, f"{plan.size:,}", date(plan.created), date(plan.modified), str(plan.source), str(plan.destination), plan.warning))
            if hasattr(result, "items"):
                self.row_items[row] = result.items[len(self.row_items)]
        offset += 150
        if offset < len(result.plans):
            self.status.set(f"분석 결과 표시 중… {min(offset, len(result.plans)):,}/{len(result.plans):,}")
            self.root.after(1, self.render, result, offset)
            return
        self.bar.configure(value=0 if result.cancelled or (result.errors and not result.plans) else 100)
        self.status.set(f"{'취소됨(부분 결과)' if result.cancelled else '분석 종료'} · 파일 {len(result.plans):,}개 · {human_size(result.total_bytes)} · 제외 {len(result.skipped):,}개 · 오류 {len(result.errors):,}개 · 실제 변경 없음")
        self.log("\n".join([*(f"오류: {e}" for e in result.errors), *(f"제외: {s}" for s in result.skipped)]))
        self.prepared = result if hasattr(result, "items") and not result.cancelled else None
        self.set_busy(False)
        self.refresh_rows()

    def refresh_rows(self):
        if not self.prepared:
            return
        for row, item in self.row_items.items():
            duplicate = self.prepared.duplicate(item)
            label = "중복 격리" if duplicate else ("중복 보존" if item.group else "날짜 이동")
            self.table.set(row, "warning", f"{'처리' if item.selected else '제외'} / {label} / {item.plan.warning}")
            self.table.set(row, "destination", str(quarantine_directory() / "<고유ID>" / "files" / item.plan.source.name) if duplicate else str(item.plan.destination))
        count, duplicates, size = self.prepared.summary()
        self.status.set(f"파일 {len(self.prepared.items)}개 · 오류 {len(self.prepared.errors)}개 · 정리 대상 {count}개 · 중복 격리 {duplicates}개 · 중복 예상 용량 {human_size(size)} (격리는 디스크 여유 공간을 늘리지 않습니다)")

    def toggle(self):
        if self.busy or not self.prepared:
            return
        for row in self.table.selection():
            item = self.row_items[row]
            if item.valid:
                item.selected = not item.selected
        self.refresh_rows()

    def keep_selected(self):
        if self.busy or not self.prepared:
            return
        rows = self.table.selection()
        if len(rows) == 1:
            self.prepared.keep(self.row_items[rows[0]].plan.source)
            self.refresh_rows()

    def organize(self):
        if self.busy or not self.prepared:
            return
        count, duplicates, size = self.prepared.summary()
        if not count:
            return
        destinations = sorted({str(i.plan.destination.parent) for i in self.prepared.items if i.selected and not self.prepared.duplicate(i)})
        text = (f"실제 처리 {count}개 (날짜 이동 {count - duplicates}개, 중복 격리 {duplicates}개)\n"
                f"중복 예상 용량: {human_size(size)}\n격리로 디스크 여유 공간은 증가하지 않습니다.\n"
                f"이동 목적지:\n" + "\n".join(destinations) + f"\n격리: {quarantine_directory()}\n실행할까요?")
        if not messagebox.askokcancel("파일 정리 최종 확인", text, parent=self.root, default="cancel"):
            return
        self.cancel.clear()
        self.set_busy(True)
        self.bar.configure(mode="indeterminate")
        self.bar.start(20)
        Thread(target=self.execute_work, args=(self.prepared,), daemon=False).start()

    def execute_work(self, prepared):
        from .organizer import Outcome
        try:
            result = execute(prepared, confirmed=True, cancel=self.cancel)
        except Exception as exc:
            result = Outcome(errors=[str(exc)])
        self.events.put(("done", result))

    def history_window(self):
        if self.busy:
            return
        store = History()
        window = tk.Toplevel(self.root)
        window.title("작업 이력 — 복원 및 격리 관리")
        window.geometry("1050x650")
        window.grab_set()
        active = [False]
        window.protocol("WM_DELETE_WINDOW", lambda: None if active[0] else window.destroy())
        jobs = store.list()
        job_list = tk.Listbox(window, height=7)
        job_list.pack(fill="x")
        for job in jobs:
            job_list.insert("end", f"{job['id']} | {job.get('started', '')} | {job['state']}" + (" (중단 감지)" if job.get("state") == "running" else ""))
        summary = tk.StringVar(value="작업을 선택하세요. Manifest가 없으면 복원 기록을 확인할 수 없습니다.")
        ttk.Label(window, textvariable=summary, wraplength=1000).pack(fill="x")
        table = ttk.Treeview(window, columns=("from", "to", "state"), show="headings", selectmode="extended")
        for key, label in (("from", "복원할 원본 위치"), ("to", "현재 보관 위치"), ("state", "복구 상태")):
            table.heading(key, text=label)
            table.column(key, width=340)
        table.pack(fill="both", expand=True)
        current = [None]

        def refresh(event=None):
            if not job_list.curselection():
                return
            job = jobs[job_list.curselection()[0]]
            current[0] = job
            table.delete(*table.get_children())
            size = 0
            count = 0
            for index, entry in enumerate(job["entries"]):
                state = store.inspect(job, entry)
                table.insert("", "end", iid=str(index), values=(entry.get("source", "?"), entry.get("destination", "?"), state))
                if entry.get("kind") == "quarantine" and Path(entry.get("destination", "")).is_file():
                    count += 1
                    size += Path(entry["destination"]).stat().st_size
            summary.set(f"작업별 격리 {count}개 / {human_size(size)} | {job.get('errors', '')} | 행 선택 후 복원/삭제하세요. 중단 기록은 현재 파일을 검증해 표시합니다.")

        def run(delete=False):
            job = current[0]
            indices = [int(i) for i in table.selection()]
            if not job or not indices:
                return
            preview_text = "\n".join(f"{job['entries'][i]['destination']} → {job['entries'][i]['source']} | {store.inspect(job, job['entries'][i])}" for i in indices)
            warning = "영구 삭제한 파일은 되돌릴 수 없습니다! 선택한 격리 파일만 삭제합니다." if delete else "복원 미리보기 — 원본 위치 충돌/내용 변경 파일은 건너뜁니다."
            if not messagebox.askokcancel("영구 삭제 최종 확인" if delete else "복원 미리보기 및 최종 확인", warning + "\n" + preview_text, parent=window, default="cancel"):
                return
            def work():
                try:
                    result = store.purge(job, indices, confirmed=True) if delete else store.restore(job, indices=indices, confirmed=True)
                    queue.put("\n".join(result))
                except Exception as exc:
                    queue.put(str(exc))
            queue = Queue()
            active[0] = True
            job_list.configure(state="disabled")
            controls.pack_forget()
            Thread(target=work, daemon=False).start()
            def poll():
                try:
                    result = queue.get_nowait()
                except Empty:
                    window.after(100, poll)
                    return
                active[0] = False
                job_list.configure(state="normal")
                refresh()
                summary.set(result)
                controls.pack(fill="x")
            window.after(100, poll)
        job_list.bind("<<ListboxSelect>>", refresh)
        controls = ttk.Frame(window)
        controls.pack(fill="x")
        ttk.Button(controls, text="선택 항목 복원…", command=run).pack(side="left")
        ttk.Button(controls, text="선택 격리 파일 영구 삭제…", command=lambda: run(True)).pack(side="left")

    def log(self, text):
        self.messages.configure(state="normal")
        self.messages.delete("1.0", "end")
        self.messages.insert("1.0", text)
        self.messages.configure(state="disabled")

    def close(self):
        if self.busy:
            self.cancel.set()
            self.status.set("취소 요청됨. 작업 종료 후 창을 닫으세요.")
            return
        self.closed = True
        self.cancel.set()
        self.root.destroy()


def main():
    configure_logging()
    root = tk.Tk()
    OrganizerApp(root)
    root.mainloop()
