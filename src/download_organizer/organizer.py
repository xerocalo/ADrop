"""미리보기와 확인된 작업 실행. 기존 대상은 절대로 덮어쓰지 않는다."""
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from collections import defaultdict
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
import uuid

from .analyzer import FilePlan, _linked, analyze

CHUNK = 1024 * 1024


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def safe_path(path: Path):
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            if _linked(part.lstat()):
                raise ValueError(f"링크/reparse 경로 거부: {part}")


def check_scope(root: Path, scope: Path):
    safe_path(root)
    if not root.is_relative_to(scope) or root == scope:
        raise ValueError("사용자 홈 아래의 개별 폴더만 작업할 수 있습니다.")
    relative = root.relative_to(scope)
    if relative.parts[0].casefold() in {"appdata", ".local", ".config", ".ssh", ".aws"}:
        raise ValueError("앱 데이터/설정 폴더는 작업 대상에서 제외합니다.")


@dataclass
class Item:
    plan: FilePlan
    snapshot: tuple
    digest: str | None = None
    group: str | None = None
    selected: bool = True
    valid: bool = True


@dataclass
class Preview:
    root: Path
    scope: Path
    items: list[Item] = field(default_factory=list)
    keepers: dict[str, Path] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def plans(self):
        return [i.plan for i in self.items]

    @property
    def total_bytes(self):
        return sum(i.plan.size for i in self.items)

    @property
    def skipped(self):
        return []

    def duplicate(self, item):
        return bool(item.group and self.keepers[item.group] != item.plan.source)

    def summary(self):
        chosen = [i for i in self.items if i.selected and i.valid]
        duplicates = [i for i in chosen if self.duplicate(i)]
        return len(chosen), len(duplicates), sum(i.plan.size for i in duplicates)

    def keep(self, path):
        item = next(i for i in self.items if i.plan.source == path)
        if item.group:
            self.keepers[item.group] = path


@contextmanager
def open_reader(path):
    if os.name != "nt":
        with path.open("rb") as stream:
            yield stream
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    handle = api.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb") as stream:
        yield stream


def digest_file(path, snapshot, cancel=None):
    value = hashlib.sha256()
    with open_reader(path) as stream:
        if signature(os.fstat(stream.fileno()))[2:] != snapshot[2:]:
            raise ValueError("분석 중 변경된 파일")
        while chunk := stream.read(CHUNK):
            if cancel and cancel.is_set():
                raise InterruptedError("분석 취소")
            value.update(chunk)
        if signature(os.fstat(stream.fileno()))[2:] != snapshot[2:]:
            raise ValueError("해시 계산 중 변경된 파일")
    if signature(path.lstat()) != snapshot:
        raise ValueError("파일 경로가 변경됨")
    return value.hexdigest()


def equal_files(a, b, cancel=None):
    with open_reader(a) as left, open_reader(b) as right:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError("분석 취소")
            x, y = left.read(CHUNK), right.read(CHUNK)
            if x != y:
                return False
            if not x:
                return True


def preview(folder, basis="created", *, scope=None, progress=None, cancel=None):
    root = Path(os.path.abspath(folder))
    scope = Path(os.path.abspath(scope or Path.home()))
    result = Preview(root, scope)
    try:
        check_scope(root, scope)
    except (OSError, ValueError) as exc:
        result.errors.append(str(exc))
        return result
    analysis = analyze(root, basis, progress=progress, cancel=cancel)
    result.errors.extend(analysis.errors)
    result.errors.extend(analysis.skipped)
    result.cancelled = analysis.cancelled
    sizes = defaultdict(list)
    for plan in analysis.plans:
        try:
            snapshot = signature(plan.source.lstat())
            if snapshot != plan.snapshot:
                raise ValueError("메타데이터 수집 후 변경됨")
            with plan.source.open("rb"):
                pass
            item = Item(plan, snapshot, selected=not bool(plan.warning))
            result.items.append(item)
            sizes[plan.size].append(item)
        except (OSError, ValueError) as exc:
            result.errors.append(f"{plan.source}: {exc}")
    for candidates in sizes.values():
        if len(candidates) < 2:
            continue
        hashes = defaultdict(list)
        for item in candidates:
            try:
                item.digest = digest_file(item.plan.source, item.snapshot, cancel)
                hashes[item.digest].append(item)
            except (OSError, ValueError) as exc:
                item.selected = False
                item.valid = False
                result.errors.append(f"{item.plan.source}: {exc}")
        for matches in hashes.values():
            # SHA 충돌 가능성까지 고려해 실제 바이트별 하위 그룹으로 나눈다.
            groups = []
            for item in matches:
                try:
                    for group in groups:
                        if equal_files(item.plan.source, group[0].plan.source, cancel):
                            group.append(item)
                            break
                    else:
                        groups.append([item])
                except (OSError, ValueError) as exc:
                    item.selected = False
                    item.valid = False
                    result.errors.append(f"{item.plan.source}: {exc}")
            for group in groups:
                if len(group) < 2:
                    continue
                key = str(uuid.uuid4())
                # 생성일 미지원 플랫폼에서는 수정일을 명시적 대체 기준으로 사용.
                keeper = min(group, key=lambda i: (i.plan.created or i.plan.modified, str(i.plan.source)))
                result.keepers[key] = keeper.plan.source
                for item in group:
                    item.group = key
    result.cancelled |= bool(cancel and cancel.is_set())
    return result


def quarantine_directory():
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "DownloadOrganizer" / "Quarantine"


@dataclass
class Outcome:
    moved: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@contextmanager
def protect_source(path):
    """Windows에서 쓰기/삭제 공유를 금지한 핸들을 작업 완료까지 유지한다."""
    if os.name != "nt":
        yield None
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    handle = api.CreateFileW(str(path), 0x80000000 | 0x10000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    with os.fdopen(fd, "rb") as stream:
        yield (api, msvcrt.get_osfhandle(stream.fileno()))


def remove_source(path, protected):
    if protected is None:
        path.unlink()
    else:
        import ctypes
        from ctypes import wintypes
        api, handle = protected
        api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetFileInformationByHandle.restype = wintypes.BOOL
        delete = ctypes.c_ubyte(1)
        if not api.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())


def execute(prepared: Preview, *, confirmed: bool, quarantine=None, cancel=None):
    outcome = Outcome()
    if not confirmed or prepared.cancelled:
        return outcome
    quarantine = Path(quarantine or quarantine_directory()).absolute()
    items = [i for i in prepared.items if i.selected]
    # 격리 전에 보존본을 검증할 수 있도록 중복부터 처리한다.
    items.sort(key=lambda i: not prepared.duplicate(i))
    for item in items:
        if cancel and cancel.is_set():
            break
        destination = None
        created_target = False
        stack = ExitStack()
        try:
            check_scope(prepared.root, prepared.scope)
            source = item.plan.source
            if not item.valid:
                raise ValueError("분석 검증 실패 항목: 다시 분석하세요.")
            if source.parent != prepared.root:
                raise ValueError("작업 범위 밖 파일")
            safe_path(source)
            protected = stack.enter_context(protect_source(source))
            if signature(source.lstat()) != item.snapshot:
                raise ValueError("미리보기 이후 파일이 변경됨")
            expected = digest_file(source, item.snapshot, cancel)
            if item.digest and expected != item.digest:
                raise ValueError("미리보기 이후 내용이 변경됨")
            duplicate = prepared.duplicate(item)
            if duplicate:
                keeper = next(i for i in prepared.items if i.plan.source == prepared.keepers[item.group])
                safe_path(keeper.plan.source)
                stack.enter_context(protect_source(keeper.plan.source))
                if digest_file(keeper.plan.source, keeper.snapshot, cancel) != expected or not equal_files(source, keeper.plan.source, cancel):
                    raise ValueError("보존 파일이 변경됨")
                safe_path(quarantine)
                if quarantine.is_relative_to(prepared.root):
                    raise ValueError("격리 경로는 작업 폴더 밖이어야 합니다.")
                destination = quarantine / str(uuid.uuid4()) / "files" / source.name
            else:
                destination = item.plan.destination
                if destination.parent.parent != prepared.root:
                    raise ValueError("작업 범위 밖 대상")
            safe_path(destination)
            if os.path.lexists(destination):
                raise FileExistsError(f"이름 충돌: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            safe_path(destination.parent)
            if duplicate:
                # 원본 경로는 이동 전에 영속화. 중단 시에도 복구 단서가 남는다.
                record = destination.parent.parent / "original.json"
                with record.open("x", encoding="utf-8") as log:
                    json.dump({"original": str(source), "quarantined": str(destination), "sha256": expected}, log, ensure_ascii=False)
                    log.flush()
                    os.fsync(log.fileno())
            with open_reader(source) as stream, destination.open("xb") as target:
                created_target = True
                copied = hashlib.sha256()
                while chunk := stream.read(CHUNK):
                    if cancel and cancel.is_set():
                        raise InterruptedError("작업 취소")
                    target.write(chunk)
                    copied.update(chunk)
                target.flush()
                os.fsync(target.fileno())
                if signature(os.fstat(stream.fileno()))[2:] != item.snapshot[2:] or copied.hexdigest() != expected:
                    raise ValueError("복사 중 원본이 변경됨")
            safe_path(destination)
            if digest_file(destination, signature(destination.lstat()), cancel) != expected:
                raise ValueError("대상 복사본 검증 실패")
            if digest_file(source, item.snapshot, cancel) != expected:
                raise ValueError("원본 변경 감지")
            if duplicate and digest_file(keeper.plan.source, keeper.snapshot, cancel) != expected:
                raise ValueError("보존 파일 변경 감지")
            os.utime(destination, ns=(source.stat().st_atime_ns, item.snapshot[3]))
            if signature(source.lstat()) != item.snapshot:
                raise ValueError("작업 직전 원본 변경 감지")
            remove_source(source, protected)
            (outcome.quarantined if duplicate else outcome.moved).append(str(destination))
        except (OSError, ValueError, StopIteration) as exc:
            outcome.errors.append(f"{item.plan.source}: {exc}")
            if created_target and destination is not None:
                try:
                    destination.unlink()
                except OSError as cleanup:
                    outcome.errors.append(f"부분 복사본 정리 실패 {destination}: {cleanup}")
        finally:
            stack.close()
    return outcome
