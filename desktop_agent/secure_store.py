from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_transform(data: bytes, *, protect: bool) -> bytes | None:
    if sys.platform != "win32":
        return None
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_char)))
    destination = _DataBlob()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)
    ]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), "Hamshmareh Desktop Agent", None, None, None, 0, ctypes.byref(destination)):
        return None
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


def save_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    protected = _dpapi_transform(token.encode("utf-8"), protect=True)
    if sys.platform == "win32" and protected is None:
        raise RuntimeError("Windows credential protection is unavailable; the device token was not stored")
    path.write_bytes(b"DPAPI1" + protected if protected is not None else b"PLAIN1" + token.encode("utf-8"))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_token(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw.startswith(b"DPAPI1"):
        clear = _dpapi_transform(raw[6:], protect=False)
        return clear.decode("utf-8") if clear else None
    if raw.startswith(b"PLAIN1"):
        return raw[6:].decode("utf-8")
    return None


def delete_token(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
