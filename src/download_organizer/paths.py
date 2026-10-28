"""Windows Known Folder API로 이동된 다운로드 폴더도 지원한다."""
import os
from pathlib import Path


def default_downloads() -> Path:
    if os.name == "nt":
        import ctypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("data", ctypes.c_ubyte * 16)]

        folder = GUID.from_buffer_copy(
            uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
        )
        result = ctypes.c_wchar_p()
        shell = ctypes.WinDLL("shell32")
        ole = ctypes.WinDLL("ole32")
        shell.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        try:
            code = shell.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(result))
            if code == 0 and result.value:
                return Path(result.value)
        finally:
            if result:
                ole.CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))
    return Path.home() / "Downloads"
