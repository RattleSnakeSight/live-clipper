# ruff: noqa: E501
"""Starting a capture reader: Streamlink runs through the interpreter (never the unsigned launcher beside it),
a reader the system refuses to start is a visible error that is retried, and a retry gets a fresh folder."""
import asyncio
import sys

from live_clipper import capture
from live_clipper.buffer import BufferManager
from live_clipper.capture import Capture, source_command


def test_streamlink_runs_as_a_module_of_this_interpreter():
    assert capture._streamlink_cmd() == [sys.executable, "-m", "streamlink"]
    prod, _ = source_command("kick", "streamer_beta", "")
    assert prod[:3] == [sys.executable, "-m", "streamlink"] and "https://kick.com/streamer_beta" in prod
    assert not any(part.endswith(("streamlink.exe", "/streamlink")) for part in prod)


def test_a_reader_the_system_refuses_is_reported_and_retried_in_a_fresh_folder(tmp_path, monkeypatch):
    said = []
    buffers = BufferManager(tmp_path, 90, 10**9)
    cap = Capture(7, "kick", "streamer_beta", "", buffers, log=lambda m, level="info": said.append((level, m)))
    stale = buffers.channel_dir(7) / "run0001"                # left by a worker that ran before this one
    stale.mkdir()
    (stale / "segments.csv").write_text("s000001.ts,0.0,2.0\n")
    tries = []

    async def refused(command, **kw):
        tries.append(command)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(capture, "_spawn_reader", refused)

    async def run():
        assert await cap._run_once() is False                 # reported, not raised: the loop keeps retrying
        assert await cap._run_once() is False

    asyncio.run(run())
    assert cap.health.state == "error" and "could not start: Access is denied" in cap.health.detail
    assert "may run on this computer" in cap.health.action_needed
    assert said and said[-1][0] == "error" and len(tries) == 2
    runs = sorted(p.name for p in buffers.channel_dir(7).iterdir())
    assert runs == ["run0001", "run0002", "run0003"]          # each try got its own folder
    assert (stale / "segments.csv").read_text() == "s000001.ts,0.0,2.0\n"   # the old index was not touched
