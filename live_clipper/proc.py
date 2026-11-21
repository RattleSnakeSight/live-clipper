"""Subprocess helpers.

Production stall (live faulthandler): the event loop was blocked inside create_subprocess_exec (capture spawn)
while faster-whisper ran. Spawning happens on the loop thread, so it must be cheap:
  * the server runs on the stdlib asyncio loop (uvicorn loop="asyncio"), not uvloop;
  * no preexec_fn: CPython can then spawn with vfork (no page-table copy of a large, model-holding process)
    instead of a full fork; lowering priority is done by exec'ing `nice -n N` instead.
Windows has no `nice`: LOW_PRIO (spread into the spawn call) starts the process below normal priority instead.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# Windows: never an MSYS/Cygwin `nice` found on PATH (it would rewrite path-like ffmpeg arguments)
_NICE = shutil.which("nice") if os.name != "nt" else None
LOW_PRIO: dict = ({"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS}
                  if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS") else {})


def niced(cmd: list[str], n: int) -> list[str]:
    """cmd run at lower CPU priority without preexec_fn (keeps the fast vfork spawn path)."""
    return [_NICE, "-n", str(int(n)), *cmd] if _NICE else list(cmd)
