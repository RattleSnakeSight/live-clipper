"""Single-instance ownership. The OS lock lives until the supervisor closes it or exits.

The guard file is never removed: replacing its inode would allow two independent locks.
supervisor.lock contains discovery metadata only, not authority to signal a process.
"""

import errno
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx

from .paths import Paths

_held: dict[tuple[Path, str], tuple] = {}
_mutex = threading.RLock()


class InstanceLockError(RuntimeError):
    """Ownership could not be established; starting another instance is unsafe."""


def process_started(pid: int) -> float | None:
    """Creation time, for recognizing recycled PIDs in pre-guard lock files only."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return ticks / 10_000_000 - 11_644_473_600
        finally:
            kernel.CloseHandle(handle)
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                                text=True, timeout=2, env={**os.environ, "LC_ALL": "C"})
        return time.mktime(time.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def pid_alive(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def system_boot_time() -> float | None:
    """A legacy lock from before this boot cannot have a surviving owner."""
    if os.name == "nt":
        import ctypes

        uptime = ctypes.WinDLL("kernel32").GetTickCount64
        uptime.restype = ctypes.c_ulonglong
        return time.time() - uptime() / 1000
    try:
        return time.time() - float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return process_started(1)  # macOS: launchd is born at boot


def _try_guard(paths: Paths, *, create: bool, role: str = "supervisor"):
    try:
        guard = (paths.state / f"{role}.guard").open("a+b" if create else "r+b")
    except FileNotFoundError:
        if not create:
            return None, False
        raise
    try:
        guard.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        guard.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return None, True
        raise
    return guard, False


def _metadata(paths: Paths, role: str = "supervisor") -> dict | None:
    try:
        info = json.loads((paths.state / f"{role}.lock").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if (not isinstance(info, dict) or type(info.get("pid")) is not int or info["pid"] <= 0
            or type(info.get("port")) is not int or not 0 < info["port"] < 65536):
        return None
    return info


def _legacy_owner(paths: Paths, info: dict | None) -> dict | None:
    if not info or info.get("lock_version") == 2 or not pid_alive(info["pid"]):
        return None
    # During an upgrade, a running older worker can prove it belongs to this data folder.
    # Health is public; status requires this installation's token, including while update-gated.
    try:
        token = (paths.state / "token").read_text().strip()
        if token:
            response = httpx.get(f"http://127.0.0.1:{info['port']}/api/status",
                                 headers={"Authorization": f"Bearer {token}"},
                                 trust_env=False, follow_redirects=False, timeout=1)
            body = response.json()
            if (response.status_code == 200 and isinstance(body, dict)
                    and body.get("version") and body.get("pid")):
                return info
    except (OSError, ValueError, httpx.HTTPError):
        pass
    written = (paths.state / "supervisor.lock").stat().st_mtime
    boot = system_boot_time()
    if boot is not None and written < boot - 2:  # allow clock sampling/filesystem precision differences
        return None
    started = process_started(info["pid"])
    if started is not None and started > written:
        return None  # no authenticated worker, and the process was born after the old lock was written
    raise InstanceLockError("An older instance cannot be verified. Stop it with its original version before "
                            "starting this copy. No process was signalled and no lock was removed.")


def read_lock(paths: Paths) -> dict | None:
    """Discover a supervisor or a surviving worker through its OS guard, never its PID alone."""
    with _mutex:
        try:
            legacy = None
            for role in ("supervisor", "worker"):
                owned = _held.get((paths.state.resolve(), role))
                if owned and owned[1]["pid"] == os.getpid():
                    return dict(owned[1])
                guard, busy = _try_guard(paths, create=False, role=role)
                if guard:
                    guard.close()
                info = _metadata(paths, role)
                if busy:
                    if info:
                        return info
                    raise InstanceLockError("Live Clipper owns the instance lock but its startup information "
                                            "is not ready. Try again; no second instance was started.")
                if role == "supervisor":
                    legacy = info
            return _legacy_owner(paths, legacy)
        except OSError as exc:
            raise InstanceLockError("Cannot read the instance lock; no second instance was started.") from exc


def _wait_guard(paths: Paths, *, create: bool, role: str):
    # Status probes hold the guard only briefly. A lifetime owner still wins after this bounded wait.
    for attempt in range(21):
        guard, busy = _try_guard(paths, create=create, role=role)
        if not busy:
            return guard, False
        if attempt < 20:
            time.sleep(0.05)
    return None, True


def acquire_lock(paths: Paths, port: int, *, role: str = "supervisor") -> bool:
    if role not in ("supervisor", "worker"):
        raise ValueError("invalid instance role")
    with _mutex:
        key = (paths.state.resolve(), role)
        if key in _held:
            return False
        guard, busy = _wait_guard(paths, create=True, role=role)
        if busy:
            return False
        try:
            if role == "supervisor":
                worker, worker_busy = _wait_guard(paths, create=False, role="worker")
                if worker:
                    worker.close()
                if worker_busy or _legacy_owner(paths, _metadata(paths)):
                    guard.close()
                    return False
            info = {"pid": os.getpid(), "port": port, "lock_version": 2,
                    "instance": uuid.uuid4().hex, "role": role}
            (paths.state / f"{role}.lock").write_text(json.dumps(info), encoding="utf-8")
            _held[key] = (guard, info)
            return True
        except BaseException:
            guard.close()
            raise


def release_lock(paths: Paths, *, role: str = "supervisor") -> None:
    with _mutex:
        owned = _held.pop((paths.state.resolve(), role), None)
        if not owned:
            return
        try:
            # A Windows reader can briefly prevent deletion. Even if it remains, the metadata is inert
            # after the guard closes. Keep the guard held throughout removal so a new owner cannot race it.
            for _ in range(50):
                try:
                    (paths.state / f"{role}.lock").unlink(missing_ok=True)
                    break
                except PermissionError:
                    time.sleep(0.1)
        finally:
            owned[0].close()
