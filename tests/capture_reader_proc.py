"""A worker stand-in for test_capture_exec: starts one capture reader (FFmpeg on a test source, forever)
the way Capture does, prints its pid, then waits to be killed. Usage: capture_reader_proc.py <ffmpeg>"""
import asyncio
import sys

from live_clipper.capture import _spawn_reader


async def main(ffmpeg: str) -> None:
    p = await _spawn_reader([ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                             "-i", "testsrc=s=64x64:r=5", "-f", "null", "-"],
                            stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    print(p.pid, flush=True)
    await asyncio.Event().wait()


asyncio.run(main(sys.argv[1]))
