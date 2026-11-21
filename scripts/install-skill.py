"""Copy the Live Clipper agent skill into your coding agent's skills folder. Standard library only.

From a checkout:
    python scripts/install-skill.py --agent claude
From the web (no checkout), the script downloads SKILL.md itself:
    python -c "import urllib.request as u; exec(u.urlopen('RAW_URL_OF_THIS_FILE').read())" --agent all
    (or save this file and run it)

Agents: claude -> ~/.claude/skills, codex -> ~/.codex/skills, qwen -> ~/.qwen/skills.
--dest DIR installs into DIR/live-clipper instead. --project installs the Claude skill into ./.claude/skills.
"""

import argparse
import sys
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/OWNER/live-clipper"
SKILL = "live-clipper"
FOLDERS = {"claude": ".claude/skills", "codex": ".codex/skills", "qwen": ".qwen/skills"}


def raw_skill_url(repo_url: str = REPO_URL) -> str:
    return repo_url.replace("https://github.com/", "https://raw.githubusercontent.com/", 1).rstrip("/") + (
        f"/main/skills/{SKILL}/SKILL.md")


def find_skill_text(local: Path | None, fetch=None) -> str:
    """SKILL.md from a checkout next to this script, else from the repository."""
    if local is not None and (local / "SKILL.md").is_file():
        return (local / "SKILL.md").read_text(encoding="utf-8")
    fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=30).read().decode("utf-8"))
    return fetch(raw_skill_url())


def targets(agent: str, dest: Path | None, project: bool, home: Path, cwd: Path) -> list[Path]:
    if dest is not None:
        return [dest / SKILL]
    names = list(FOLDERS) if agent == "all" else [agent]
    return [(cwd if project and n == "claude" else home) / FOLDERS[n] / SKILL for n in names]


def install(text: str, folders: list[Path]) -> list[Path]:
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return folders


def main(argv: list[str] | None = None, fetch=None) -> int:
    p = argparse.ArgumentParser(description="Install the Live Clipper agent skill.")
    p.add_argument("--agent", choices=[*FOLDERS, "all"], default="claude")
    p.add_argument("--dest", type=Path, help="skills folder to use instead of the agent's default")
    p.add_argument("--project", action="store_true", help="install the Claude skill into ./.claude/skills")
    args = p.parse_args(argv)
    here = Path(__file__).resolve().parent.parent / "skills" / SKILL if "__file__" in globals() else None
    try:
        text = find_skill_text(here, fetch)
    except OSError as e:
        print(f"Could not get the skill file: {e}", file=sys.stderr)
        return 1
    for folder in install(text, targets(args.agent, args.dest, args.project, Path.home(), Path.cwd())):
        print(f"Installed {folder / 'SKILL.md'}")
    print("Restart your agent so it picks the skill up. "
          "Codex and Qwen Code also read AGENTS.md in the repository.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
