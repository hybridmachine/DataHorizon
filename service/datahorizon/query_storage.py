# SPDX-License-Identifier: GPL-3.0-only
"""Private query directories and conservative cleanup after a terminated service."""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid


def alive(pid):
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)];kernel.GetExitCodeProcess.restype=wintypes.BOOL
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()!=87  # Only a proven missing PID is removable.
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except (PermissionError,OSError):return True
    return True


def cleanup_terminated():
    root=Path(tempfile.gettempdir())
    for directory in root.glob('datahorizon-query-*'):
        match=re.fullmatch(r'datahorizon-query-([1-9][0-9]*)-([0-9a-f]{32})-[A-Za-z0-9_-]+',directory.name)
        if match is None or directory.is_symlink():continue
        try:
            stat=directory.stat()
            if hasattr(os,'getuid') and stat.st_uid!=os.getuid():continue
            marker=directory/'owner.json'
            if marker.is_symlink() or marker.stat().st_size>4096:continue
            owner=json.loads(marker.read_text())
            if owner!=dict(pid=int(match[1]),nonce=match[2]) or alive(owner['pid']):continue
            shutil.rmtree(directory)
        except (OSError,ValueError,TypeError):continue


def create():
    nonce=uuid.uuid4().hex;directory=tempfile.TemporaryDirectory(prefix=f'datahorizon-query-{os.getpid()}-{nonce}-')
    try:
        marker=Path(directory.name)/'owner.json';marker.write_text(json.dumps(dict(pid=os.getpid(),nonce=nonce)));marker.chmod(0o600)
        return directory
    except BaseException:directory.cleanup();raise
