"""영속 작업 저널. 손상된 기록은 실행하지 않고 사용자에게 표시한다."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import uuid


def app_data():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "DownloadOrganizer"


def now():
    return datetime.now(timezone.utc).isoformat()


def log_event(message):
    try:
        logger = logging.getLogger("download_organizer")
        logger.info(message)
    except Exception:
        pass  # 진단 로그는 작업 저널을 대체하지 않는다.


def configure_logging(base=None):
    logger = logging.getLogger("download_organizer")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        folder = Path(base or app_data()) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(folder / "organizer.log", maxBytes=2 * 1024 * 1024,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except Exception:
        logger.addHandler(logging.NullHandler())
    return logger


def checksum(path):
    value = hashlib.sha256()
    from .organizer import open_reader
    with open_reader(Path(path)) as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


class ManifestError(OSError):
    pass


class History:
    def __init__(self, base=None):
        self.base = Path(base or app_data()) / "history"

    def save(self, job):
        try:
            self._save(job)
        except (OSError, ValueError) as exc:
            raise ManifestError(str(exc)) from exc

    def _save(self, job):
        from .organizer import safe_path
        safe_path(self.base)
        self.base.mkdir(parents=True, exist_ok=True)
        target = self.base / (job["id"] + ".json")
        temporary = self.base / (job["id"] + "." + uuid.uuid4().hex + ".tmp")
        data = json.dumps(job, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
                api.MoveFileExW.restype = wintypes.BOOL
                if not api.MoveFileExW(str(temporary), str(target), 0x1 | 0x8):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                os.replace(temporary, target)
            if os.name != "nt":
                fd = os.open(self.base, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if target.read_bytes() != data:
                raise OSError("Manifest 저장 확인 실패")
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, root, quarantine):
        job = dict(version=1, id=uuid.uuid4().hex, started=now(), ended=None,
                   root=str(root), quarantine=str(quarantine), entries=[], state="running")
        self.save(job)
        return job

    def list(self):
        jobs = []
        if not self.base.exists():
            return jobs
        for path in self.base.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job.get("version") != 1 or job.get("id") != path.stem or not isinstance(job.get("entries"), list):
                    raise ValueError("Manifest 형식 오류")
                if job.get("state") not in ("running", "finished") or not all(isinstance(e, dict) for e in job["entries"]):
                    raise ValueError("Manifest 상태/항목 오류")
                jobs.append(job)
            except (OSError, ValueError, TypeError) as exc:
                jobs.append(dict(id=path.stem, state="damaged", errors=str(exc), entries=[]))
        return sorted(jobs, key=lambda j: j.get("started", ""), reverse=True)

    def inspect(self, job, entry):
        from .organizer import safe_path, check_scope
        try:
            source, destination = Path(entry["source"]), Path(entry["destination"])
            root = Path(job["root"])
            check_scope(root, Path(job.get("scope", Path.home())))
            if source.parent != root:
                raise ValueError("원본 범위 오류")
            if entry["kind"] == "quarantine":
                if not destination.is_relative_to(Path(job["quarantine"])):
                    raise ValueError("격리 범위 오류")
            elif destination.parent.parent != root:
                raise ValueError("대상 범위 오류")
            safe_path(source)
            safe_path(destination)
            if entry.get("status") == "purged":
                return "복구 불가: 영구 삭제"
            if source.exists():
                if not destination.exists() and checksum(source) == entry["hash"] and entry.get("status") in ("restored", "restoring"):
                    return "복원됨"
                return "충돌: 원본 위치 사용 중 (양쪽 파일은 자동 제거하지 않음)"
            if not destination.is_file():
                return "복구 불가: 대상 파일 없음"
            if destination.stat().st_size != entry["size"] or checksum(destination) != entry["hash"]:
                return "충돌: 대상 내용 변경"
            return "복원 가능"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return f"오류: {exc}"

    def restore(self, job, *, confirmed=False, indices=None):
        from .organizer import protect_source, remove_source, safe_path, open_reader
        if not confirmed or job.get("state") == "damaged":
            return []
        results = []
        for index, entry in enumerate(job["entries"]):
            if indices is not None and index not in indices:
                continue
            state = self.inspect(job, entry)
            if state != "복원 가능":
                results.append(state)
                continue
            source, destination = Path(entry["source"]), Path(entry["destination"])
            created = False
            try:
                entry["undo_started"] = now()
                entry["status"] = "restoring"
                self.save(job)  # 실패 시 파일에 손대지 않는다.
                with protect_source(destination) as protected:
                    if self.inspect(job, entry) != "복원 가능":
                        raise ValueError("복원 직전 상태 변경")
                    safe_path(source.parent)
                    with open_reader(destination) as incoming, source.open("xb") as outgoing:
                        created = True
                        while chunk := incoming.read(1024 * 1024):
                            outgoing.write(chunk)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if checksum(source) != entry["hash"]:
                        raise ValueError("복원 복사본 검증 실패")
                    if checksum(destination) != entry["hash"]:
                        raise ValueError("복원 중 대상 변경")
                    remove_source(destination, protected)
                entry["status"] = "restored"
                entry["undo_result"] = "success"
                results.append("복원됨")
            except (OSError, ValueError) as exc:
                # 실패 시 복사본도 보존한다. 어느 쪽도 자동 제거하지 않는다.
                entry["undo_result"] = str(exc)
                results.append(f"복원 실패: {exc}; 복사본 유지={created}")
                log_event(results[-1])
            entry["undo_ended"] = now()
            self.save(job)
        log_event(f"복원 {job['id']}: {results}")
        return results

    def purge(self, job, indices, *, confirmed=False):
        from .organizer import protect_source, remove_source
        if not confirmed:
            return []
        results = []
        for index in indices:
            entry = job["entries"][index]
            if entry["kind"] != "quarantine" or self.inspect(job, entry) != "복원 가능":
                results.append("삭제 거부: 격리 검증 실패")
                continue
            try:
                entry["delete_started"] = now()
                self.save(job)
                path = Path(entry["destination"])
                with protect_source(path) as protected:
                    if self.inspect(job, entry) != "복원 가능":
                        raise ValueError("삭제 직전 상태 변경")
                    remove_source(path, protected)
                entry["status"] = "purged"
                self.save(job)
                results.append("영구 삭제됨: 복구 불가")
            except (OSError, ValueError) as exc:
                results.append(str(exc))
        return results
