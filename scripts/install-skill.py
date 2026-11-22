"""Copy the Live Clipper agent skill into your coding agent's skills folder. Standard library only.

Run it from a checkout of the repository (no network access is used):
    python scripts/install-skill.py --agent claude

Agents: claude -> ~/.claude/skills, codex -> ~/.codex/skills, qwen -> ~/.qwen/skills.
--dest DIR installs into DIR/live-clipper instead. --project installs the Claude skill into ./.claude/skills.
With the app installed, `live-clipper skill install` does the same.
"""

import argparse
import sys
from pathlib import Path

SKILL = "live-clipper"
FOLDERS = {"claude": ".claude/skills", "codex": ".codex/skills", "qwen": ".qwen/skills"}
SOURCE = Path(__file__).resolve().parent.parent / "skills" / SKILL / "SKILL.md"


def targets(agent: str, dest: Path | None, project: bool, home: Path, cwd: Path) -> list[Path]:
    if dest is not None:
        return [dest / SKILL]
    if project and agent != "claude":  # never fall back to the home folder when this folder was asked for
        raise SystemExit("--project installs the Claude skill into this folder only. Use it with "
                         "--agent claude, and install the other agents without --project.")
    names = list(FOLDERS) if agent == "all" else [agent]
    return [(cwd if project and n == "claude" else home) / FOLDERS[n] / SKILL for n in names]


def install(text: str, folders: list[Path]) -> list[Path]:
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return folders


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Install the Live Clipper agent skill from this checkout.")
    p.add_argument("--agent", choices=[*FOLDERS, "all"], default="claude")
    p.add_argument("--dest", type=Path, help="skills folder to use instead of the agent's default")
    p.add_argument("--project", action="store_true", help="install the Claude skill into ./.claude/skills")
    args = p.parse_args(argv)
    if not SOURCE.is_file():
        print(f"Skill file not found: {SOURCE}. Run this script from a checkout of the repository.",
              file=sys.stderr)
        return 1
    text = SOURCE.read_text(encoding="utf-8")
    for folder in install(text, targets(args.agent, args.dest, args.project, Path.home(), Path.cwd())):
        print(f"Installed {folder / 'SKILL.md'}")
    print("Restart your agent so it picks the skill up. "
          "Codex and Qwen Code also read AGENTS.md in the repository.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
