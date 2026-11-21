"""Capture readers (streamlink/FFmpeg) never outlive a killed worker: a Job Object on Windows, the
parent-death signal on Linux, a parent-death watchdog elsewhere."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                          # a zombie waiting for its (new) parent is gone too
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")
def test_killing_the_worker_kills_its_capture_reader():
    proc = str(Path(__file__).with_name("capture_reader_proc.py"))
    worker = subprocess.Popen([sys.executable, proc, FFMPEG], stdout=subprocess.PIPE, text=True)
    child = int(worker.stdout.readline())
    try:
        assert alive(child)
        worker.kill()                             # no cleanup code runs in the worker
        worker.wait(10)
        end = time.time() + 10
        while alive(child) and time.time() < end:
            time.sleep(0.1)
        assert not alive(child), "the capture reader outlived its worker"
    finally:
        if alive(child):
            os.kill(child, 9)
