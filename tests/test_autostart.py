import plistlib
import shlex
import xml.etree.ElementTree as ET

from live_clipper import autostart

CMD = ["/opt/my apps/python3", "-m", "live_clipper", "start", "--no-browser"]
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_launch_command_starts_without_browser(monkeypatch):
    monkeypatch.delenv("LIVE_CLIPPER_HOME")
    cmd = autostart.launch_command()
    assert cmd[1:] == ["-m", "live_clipper", "start", "--no-browser"]


def test_launch_command_carries_custom_data_dir(home):
    cmd = autostart.launch_command()
    assert cmd[1:] == ["-m", "live_clipper", "--home", str(home), "start", "--no-browser"]
    stop = shlex.split(autostart.render_systemd(cmd).split("ExecStop=")[1].splitlines()[0])
    assert stop[-3:] == ["--home", str(home), "stop"]


def test_systemd_user_unit():
    text = autostart.render_systemd(CMD)
    exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(exec_start.removeprefix("ExecStart=")) == CMD
    assert "WantedBy=default.target" in text
    assert "ExecStop=" in text


def test_launchd_plist():
    plist = plistlib.loads(autostart.render_launchd(CMD).encode())
    assert plist["ProgramArguments"] == CMD
    assert plist["RunAtLoad"] is True
    assert plist["Label"] == autostart.LABEL


def test_windows_task_xml():
    cmd = [r"C:\Program Files\Py\pythonw.exe", "-m", "live_clipper", "start", "--no-browser"]
    root = ET.fromstring(autostart.render_task_xml(cmd, user="PC\\someone"))
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == "PC\\someone"
    assert root.find("t:Actions/t:Exec/t:Command", NS).text == cmd[0]
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "-m live_clipper start --no-browser"
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
