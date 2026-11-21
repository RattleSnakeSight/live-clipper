"""Optional start at login: Windows Task Scheduler, macOS launchd LaunchAgent, Linux systemd --user unit.

If LIVE_CLIPPER_HOME is set when the unit is installed, it is carried into the unit as `--home`.
"""

import getpass
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "live-clipper"
TASK_NAME = "Live Clipper"


def launch_command() -> list[str]:
    exe = Path(sys.executable)
    if os.name == "nt" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"  # no console window flash at login
    home = os.environ.get("LIVE_CLIPPER_HOME")
    return [str(exe), "-m", "live_clipper", *(["--home", home] if home else []), "start", "--no-browser"]


def render_systemd(cmd: list[str]) -> str:
    stop = [*cmd[:-2], "stop"]
    return f"""[Unit]
Description=Live Clipper

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={shlex.join(cmd)}
ExecStop={shlex.join(stop)}

[Install]
WantedBy=default.target
"""


def render_launchd(cmd: list[str]) -> str:
    # AbandonProcessGroup: `start` exits after spawning the supervisor; launchd must not kill it.
    return plistlib.dumps({"Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True,
                           "AbandonProcessGroup": True}).decode()


def render_task_xml(cmd: list[str], user: str) -> str:
    user, command, args = escape(user), escape(cmd[0]), escape(subprocess.list2cmdline(cmd[1:]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Start Live Clipper at login</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{args}</Arguments></Exec></Actions>
</Task>
"""


def _systemd_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "systemd" / "user" / f"{LABEL}.service"


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _run(*args: str) -> int:
    return subprocess.run(args, capture_output=True).returncode


def install() -> None:
    cmd = launch_command()
    if os.name == "nt":
        user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
        xml = Path(os.environ.get("TEMP", ".")) / "live-clipper-task.xml"
        xml.write_text(render_task_xml(cmd, user), encoding="utf-16")
        try:
            subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"], check=True)
        finally:
            xml.unlink(missing_ok=True)
    elif sys.platform == "darwin":
        path = _launchd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_launchd(cmd))
        _run("launchctl", "bootout", f"gui/{os.getuid()}", str(path))
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    else:
        path = _systemd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_systemd(cmd))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", path.name], check=True)


def uninstall() -> None:
    if os.name == "nt":
        _run("schtasks", "/Delete", "/TN", TASK_NAME, "/F")
    elif sys.platform == "darwin":
        _run("launchctl", "bootout", f"gui/{os.getuid()}", str(_launchd_path()))
        _launchd_path().unlink(missing_ok=True)
    else:
        _run("systemctl", "--user", "disable", _systemd_path().name)
        _systemd_path().unlink(missing_ok=True)
        _run("systemctl", "--user", "daemon-reload")


def is_installed() -> bool:
    if os.name == "nt":
        return _run("schtasks", "/Query", "/TN", TASK_NAME) == 0
    return (_launchd_path() if sys.platform == "darwin" else _systemd_path()).exists()
