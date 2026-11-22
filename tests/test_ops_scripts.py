import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_installer():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("install_skill", ROOT / "scripts" / "install-skill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_skill_frontmatter_is_discoverable():
    text = (ROOT / "live_clipper" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    assert "name: live-clipper" in front
    desc = re.search(r"^description: (.+)$", front, re.M).group(1).lower()
    for word in ("install", "set up", "troubleshoot", "launch"):
        assert word in desc
    line = r"^`REPO_URL = https://github.com/RattleSnakeSight/live-clipper`$"
    assert len(re.findall(line, text, re.M)) == 1
    assert text.count("https://github.com/") == 1


def test_install_skill_from_checkout_for_each_agent(tmp_path, monkeypatch):
    mod = load_installer()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "h"))
    assert mod.main(["--agent", "all"]) == 0
    for folder in (".claude", ".codex", ".qwen"):
        installed = tmp_path / "h" / folder / "skills" / "live-clipper" / "SKILL.md"
        assert installed.read_text(encoding="utf-8").startswith("---\nname: live-clipper")
    assert mod.main(["--agent", "claude"]) == 0  # idempotent


def test_install_skill_dest_override_and_project(tmp_path, monkeypatch):
    mod = load_installer()
    dest = tmp_path / "custom"
    assert mod.main(["--agent", "codex", "--dest", str(dest)]) == 0
    assert (dest / "live-clipper" / "SKILL.md").is_file()
    monkeypatch.chdir(tmp_path)
    assert mod.main(["--project"]) == 0
    assert (tmp_path / ".claude" / "skills" / "live-clipper" / "SKILL.md").is_file()


@pytest.mark.parametrize("agent", ["all", "codex", "qwen"])
def test_project_scope_is_refused_for_anything_but_claude(agent, tmp_path, monkeypatch):
    """--project means this folder only: no agent's skill may land in the home folder instead."""
    from live_clipper.cli import main
    home = tmp_path / "h"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(tmp_path)
    for run in (lambda: load_installer().main(["--project", "--agent", agent]),
                lambda: main(["skill", "install", "--project", "--agent", agent])):
        with pytest.raises(SystemExit) as stop:
            run()
        assert "--project" in str(stop.value)
    assert not home.exists() and not (tmp_path / ".claude").exists()  # nothing was written anywhere


def test_repo_and_packaged_skill_are_identical():
    repo = (ROOT / "skills" / "live-clipper" / "SKILL.md").read_bytes()
    assert repo == (ROOT / "live_clipper" / "skill" / "SKILL.md").read_bytes()


def test_cli_skill_install_and_path(tmp_path, monkeypatch, capsys):
    from live_clipper.cli import main
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "h"))
    assert main(["skill", "install", "--agent", "all"]) == 0
    for folder in (".claude", ".codex", ".qwen"):
        installed = tmp_path / "h" / folder / "skills" / "live-clipper" / "SKILL.md"
        assert installed.read_text(encoding="utf-8").startswith("---" + chr(10) + "name: live-clipper")
    dest = tmp_path / "custom"
    assert main(["skill", "install", "--agent", "codex", "--dest", str(dest)]) == 0
    assert (dest / "live-clipper" / "SKILL.md").is_file()
    monkeypatch.chdir(tmp_path)
    assert main(["skill", "install", "--project"]) == 0
    assert (tmp_path / ".claude" / "skills" / "live-clipper" / "SKILL.md").is_file()
    capsys.readouterr()
    assert main(["skill", "path"]) == 0
    assert Path(capsys.readouterr().out.strip()).is_file()


def test_install_skill_script_uses_no_network():
    text = (ROOT / "scripts" / "install-skill.py").read_text(encoding="utf-8")
    assert "urllib" not in text and "raw.githubusercontent" not in text


def test_one_repo_url_constant_per_install_script():
    for name in ("install.sh", "install.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert text.count("https://github.com/RattleSnakeSight/live-clipper") == 1


def test_install_scripts_exist_and_parse():
    sh = ROOT / "scripts" / "install.sh"
    ps = ROOT / "scripts" / "install.ps1"
    assert "git+$REPO_URL" in sh.read_text() and "git+$RepoUrl" in ps.read_text()
    if shutil.which("sh"):
        assert subprocess.run(["sh", "-n", str(sh)]).returncode == 0


def test_no_personal_paths_in_ops_files():
    pattern = re.compile(r"[A-Za-z]:\\Users\\|/home/\w+|/Users/\w+")
    files = [*ROOT.glob("live_clipper/guide/*.md"), ROOT / "AGENTS.md", ROOT / ".env.example",
             *ROOT.glob("scripts/*.*"), ROOT / "live_clipper" / "skill" / "SKILL.md"]
    for f in files:
        assert not pattern.search(f.read_text(encoding="utf-8")), f


@pytest.mark.parametrize("slug", ["install", "privacy", "troubleshooting"])
def test_guide_pages_have_no_em_dashes(slug):
    assert "—" not in (ROOT / "live_clipper" / "guide" / f"{slug}.md").read_text(encoding="utf-8")
