"""Tie capture readers (streamlink, FFmpeg) to the worker that spawned them, so a killed worker leaves none behind.

- Linux: run as a script, this child sets PR_SET_PDEATHSIG and replaces itself with the reader (execv).
- macOS and other POSIX systems: run as a script with --watch, this child starts the reader, passes SIGTERM on
  and kills the reader as soon as its parent is gone (os.getppid() changes when the parent dies).
- Windows: `bind_to_kill_job(pid)` puts a reader started suspended into a Job Object with
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, then resumes it; the job handle closes when the worker dies.
"""
import ctypes
import os
import signal
import sys

CREATE_SUSPENDED = 0x00000004
_JOB = None


def _kernel32():
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32


def _kill_job(k32):
    """The worker's Job Object (created once; never inherited, closed by the OS when the worker dies)."""
    global _JOB
    if _JOB is None:
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # extended limits
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            raise ctypes.WinError(err)
        _JOB = job
    return _JOB


def bind_to_kill_job(pid: int) -> None:
    """Windows: put a process started with CREATE_SUSPENDED into the worker's kill-on-close job, then resume it
    (it is resumed even when binding fails, so a capture never hangs)."""
    k32 = _kernel32()
    h = k32.OpenProcess(0x0100 | 0x0800 | 0x0001, False, pid)  # SET_QUOTA | SUSPEND_RESUME | TERMINATE
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not k32.AssignProcessToJobObject(_kill_job(k32), h):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = (ctypes.c_void_p,)
        ntdll.NtResumeProcess(h)
        k32.CloseHandle(h)


def _exec_with_pdeathsig(expected_parent: int, command: list[str]) -> None:
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    PR_SET_PDEATHSIG = 1
    if prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    # Death before prctl would have sent no signal. Never start a reader in that case.
    if os.getppid() != expected_parent:
        sys.exit(1)
    os.execv(command[0], command)


def _watch(expected_parent: int, command: list[str]) -> int:
    """Run the reader as a child (same stdin/stdout/stderr) and kill it once the parent is gone."""
    import subprocess

    if os.getppid() != expected_parent:
        return 1
    child = subprocess.Popen(command)
    signal.signal(signal.SIGTERM, lambda *_: child.terminate())
    while True:
        try:
            rc = child.wait(0.5)
            return rc if rc >= 0 else 128 - rc
        except subprocess.TimeoutExpired:
            if os.getppid() != expected_parent:  # re-parented: the worker died
                child.kill()
                child.wait()
                return 1


if __name__ == "__main__":
    if sys.argv[1] == "--watch":
        sys.exit(_watch(int(sys.argv[2]), sys.argv[3:]))
    _exec_with_pdeathsig(int(sys.argv[1]), sys.argv[2:])
