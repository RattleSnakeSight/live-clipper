"""Documentation checks: links resolve, the in-app guide uses only what its renderer supports, and the tester
prompt is the same in the README and the Install page."""
import re
from pathlib import Path

import pytest

from live_clipper.setup_service import GUIDE_DIR, GUIDE_PAGES

ROOT = Path(__file__).resolve().parents[1]
SLUGS = [s for s, _ in GUIDE_PAGES]
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
PUBLIC_DOCS = [ROOT / n for n in ("README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md", "PROGRESS.md")]
PUBLIC_DOCS += [*sorted((ROOT / "docs").glob("*.md")), *(GUIDE_DIR / f"{s}.md" for s in SLUGS)]


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def prose(path: Path) -> str:
    """The page without fenced blocks and inline code, where link syntax is only an example."""
    no_fences = re.sub(r"^\s*```.*?^\s*```", "", text(path), flags=re.S | re.M)
    return re.sub(r"`[^`\n]+`", "", no_fences)


def anchors(path: Path) -> set[str]:
    """GitHub heading anchors: lower case, punctuation dropped, spaces to hyphens."""
    out = set()
    for line in text(path).splitlines():
        m = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            out.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", m.group(1).lower())))
    return out


def test_every_guide_page_is_registered_and_present():
    assert sorted(SLUGS) == sorted(p.stem for p in GUIDE_DIR.glob("*.md"))


@pytest.mark.parametrize("doc", PUBLIC_DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(doc):
    for _, href in LINK.findall(prose(doc)):
        if re.match(r"[a-z]+:", href):
            continue
        target, _, anchor = href.partition("#")
        dest = (doc.parent / target).resolve() if target else doc
        assert dest.exists(), f"{doc.name}: broken link {href}"
        if anchor and dest.suffix == ".md":
            assert anchor in anchors(dest), f"{doc.name}: no heading for #{anchor} in {dest.name}"


@pytest.mark.parametrize("slug", SLUGS)
def test_guide_page_fits_the_in_app_renderer(slug):
    """web/js/md.js renders [text](url) links and maps a sibling `x.md` to its guide page, dropping any
    #anchor; other relative links go to the guide index. No `<url>` autolinks; a fenced block ends a list."""
    page = text(GUIDE_DIR / f"{slug}.md")
    assert "—" not in page, "em dash"
    assert not re.search(r"<https?://", page), "autolinks render as plain text in the app: use [text](url)"
    for _, href in LINK.findall(prose(GUIDE_DIR / f"{slug}.md")):
        if re.match(r"https?://", href):
            continue
        m = re.fullmatch(r"([\w-]+)\.md", href)
        assert m and m.group(1) in SLUGS, f"guide links must name a sibling page, no anchor: {href}"
    lines = page.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s+(```|~~~)", line):
            raise AssertionError(f"line {i + 1}: an indented code block breaks the list around it in the app")


def prompt(path: Path) -> str:
    m = re.search(r"```text\n(Install Live Clipper for me.*?)\n```", text(path), re.S)
    assert m, f"no tester prompt in {path.name}"
    return m.group(1)


def test_tester_prompt_is_the_same_in_readme_and_install_page():
    readme = prompt(ROOT / "README.md")
    assert readme == prompt(GUIDE_DIR / "install.md")
    for needed in ("pipx install git+https://github.com/RattleSnakeSight/live-clipper",
                   "live-clipper skill install --agent claude", "live-clipper doctor", "live-clipper start",
                   "Manual posting", "test destination", "permission to clip", "live-clipper agent pair",
                   "Never read, print or ask me for key values"):
        assert needed in readme, needed
