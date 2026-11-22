"""Copy the packaged agent skill into a coding agent's skills folder."""

from pathlib import Path

SKILL = "live-clipper"
FOLDERS = {"claude": ".claude/skills", "codex": ".codex/skills", "qwen": ".qwen/skills"}
SOURCE = Path(__file__).resolve().parent / "skill" / "SKILL.md"


def targets(agent: str, dest: Path | None, project: bool, home: Path, cwd: Path) -> list[Path]:
    if dest is not None:
        return [dest / SKILL]
    if project and agent != "claude":  # never fall back to the home folder when this folder was asked for
        raise SystemExit("--project installs the Claude skill into this folder only. Use it with "
                         "--agent claude, and install the other agents without --project.")
    names = list(FOLDERS) if agent == "all" else [agent]
    return [(cwd if project and n == "claude" else home) / FOLDERS[n] / SKILL for n in names]


def install(folders: list[Path]) -> list[Path]:
    text = SOURCE.read_text(encoding="utf-8")
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return folders
