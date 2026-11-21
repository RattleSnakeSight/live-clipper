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
    text = (ROOT / "skills" / "live-clipper" / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    assert "name: live-clipper" in front
    desc = re.search(r"^description: (.+)$", front, re.M).group(1).lower()
    for word in ("install", "set up", "troubleshoot", "launch"):
        assert word in desc
    assert len(re.findall(r"^`REPO_URL = https://github.com/OWNER/live-clipper`$", text, re.M)) == 1
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


def test_install_skill_downloads_when_no_checkout(tmp_path):
    mod = load_installer()
    urls = []
    body = "---\nname: live-clipper\n---\n"
    text = mod.find_skill_text(tmp_path / "missing", fetch=lambda u: urls.append(u) or body)
    assert text.startswith("---") and urls[0].startswith("https://raw.githubusercontent.com/OWNER/live-clipper/")
    assert urls[0].endswith("/skills/live-clipper/SKILL.md")


def test_install_scripts_exist_and_parse():
    sh = ROOT / "scripts" / "install.sh"
    ps = ROOT / "scripts" / "install.ps1"
    assert "git+$REPO_URL" in sh.read_text() and "git+$RepoUrl" in ps.read_text()
    if shutil.which("sh"):
        assert subprocess.run(["sh", "-n", str(sh)]).returncode == 0


def test_no_personal_paths_in_ops_files():
    pattern = re.compile(r"[A-Za-z]:\\Users\\|/home/\w+|/Users/\w+")
    files = [*ROOT.glob("live_clipper/guide/*.md"), ROOT / "AGENTS.md", ROOT / ".env.example",
             *ROOT.glob("scripts/*.*"), ROOT / "skills" / "live-clipper" / "SKILL.md"]
    for f in files:
        assert not pattern.search(f.read_text(encoding="utf-8")), f


@pytest.mark.parametrize("slug", ["install", "privacy", "troubleshooting"])
def test_guide_pages_have_no_em_dashes(slug):
    assert "—" not in (ROOT / "live_clipper" / "guide" / f"{slug}.md").read_text(encoding="utf-8")
