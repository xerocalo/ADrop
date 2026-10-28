"""읽기 전용 분석. 파일 이동/삭제/디렉터리 생성 API를 사용하지 않는다."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable, Literal
import os
import re
import stat

DateBasis = Literal["modified", "created"]
PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp", ".temp")
DATE_FOLDER = re.compile(r"\d{2}\.\d{2}\.\d{2}\Z")


@dataclass(frozen=True)
class FilePlan:
    source: Path
    destination: Path
    size: int
    created: datetime | None
    modified: datetime
    warning: str = ""


@dataclass(frozen=True)
class Progress:
    processed: int
    total: int
    files: int
    bytes: int


@dataclass
class AnalysisResult:
    plans: list[FilePlan] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(plan.size for plan in self.plans)


def _linked(info: os.stat_result) -> bool:
    # Windows junction 및 기타 reparse point도 따라가지 않는다.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def analyze(folder: Path, basis: DateBasis = "modified", *,
            progress: Callable[[Progress], None] | None = None,
            cancel: Event | None = None) -> AnalysisResult:
    result = AnalysisResult()
    try:
        if basis not in ("modified", "created"):
            raise ValueError("지원하지 않는 날짜 기준입니다.")
        root = Path(os.path.abspath(folder.expanduser()))
        # 경로 중간의 링크도 금지: resolve() 전에 검사한다.
        for part in (root, *root.parents):
            if _linked(part.lstat()):
                raise ValueError("심볼릭 링크 또는 reparse point 경로는 분석할 수 없습니다.")
        if any(DATE_FOLDER.fullmatch(part) for part in root.parts):
            raise ValueError("날짜별 정리 폴더 내부는 재분석하지 않습니다.")
        if not root.is_dir():
            raise ValueError("분석할 폴더가 아닙니다.")
        with os.scandir(root) as scan:
            entries = list(scan)
    except (OSError, ValueError) as exc:
        result.errors.append(str(exc))
        return result

    size = 0
    if progress:
        progress(Progress(0, len(entries), 0, 0))
    for index, entry in enumerate(entries, 1):
        if cancel and cancel.is_set():
            result.cancelled = True
            break
        path = root / entry.name
        try:
            info = entry.stat(follow_symlinks=False)
            if _linked(info) or not stat.S_ISREG(info.st_mode):
                result.skipped.append(f"{entry.name}: 폴더 또는 링크/특수 파일")
            elif entry.name.lower().endswith(PARTIAL_SUFFIXES) or entry.name.startswith("~$"):
                result.skipped.append(f"{entry.name}: 다운로드/작업 중 임시 파일")
            else:
                modified = datetime.fromtimestamp(info.st_mtime)
                # Unix ctime은 생성일이 아니므로 생성일로 표시하지 않는다.
                birth = getattr(info, "st_birthtime", None)
                if birth is None and os.name == "nt":
                    birth = info.st_ctime
                created = datetime.fromtimestamp(birth) if birth is not None else None
                chosen = modified if basis == "modified" else created
                if chosen is None:
                    raise ValueError("이 플랫폼은 생성일을 제공하지 않습니다. 수정일을 선택하세요.")
                target_folder = root / chosen.strftime("%y.%m.%d")
                destination = target_folder / entry.name
                warning = ""
                if os.path.lexists(target_folder):
                    target_info = target_folder.lstat()
                    if _linked(target_info) or not stat.S_ISDIR(target_info.st_mode):
                        warning = "예상 대상 폴더가 링크 또는 일반 파일입니다."
                    elif os.path.lexists(destination):
                        warning = "예상 대상에 같은 이름이 있습니다."
                result.plans.append(FilePlan(path, destination, info.st_size, created, modified, warning))
                size += info.st_size
        except (OSError, ValueError, OverflowError) as exc:
            result.errors.append(f"{entry.name}: {exc}")
        if progress:
            progress(Progress(index, len(entries), len(result.plans), size))
    return result
