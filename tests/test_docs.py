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


# ---- the account instructions in the app and in the guide say the same thing ------------------------------

OFFICIAL_HOSTS = {
    "console.cloud.google.com", "developers.google.com", "support.google.com", "myaccount.google.com",
    "dev.twitch.tv", "www.twitch.tv", "kick.com", "developers.tiktok.com", "developers.facebook.com",
    "help.metricool.com",
}


def _links(node):
    """Every {"label", "url"} link and every bare docs URL inside the instruction data."""
    if isinstance(node, dict):
        if set(node) == {"label", "url"}:
            yield node["url"]
        else:
            for k, v in node.items():
                if k != "scopes":  # permission names that look like addresses
                    yield from _links(v)
    elif isinstance(node, list):
        for v in node:
            yield from _links(v)
    elif isinstance(node, str) and node.startswith("http") and " " not in node:
        yield node


def test_instruction_links_go_to_the_providers_own_sites():
    from urllib.parse import urlsplit

    from live_clipper.publish.requirements import REQUIREMENTS
    from live_clipper.sources.guide import GUIDE

    urls = set(_links(GUIDE)) | set(_links({k: v for k, v in REQUIREMENTS.items()}))
    urls = {u for u in urls if "{port}" not in u}  # the app's own callback addresses are not links
    assert len(urls) > 15
    for u in urls:
        parts = urlsplit(u)
        assert parts.scheme == "https" and parts.hostname in OFFICIAL_HOSTS, u


def test_the_guide_names_what_the_account_pages_show():
    from live_clipper.publish.requirements import AS_OF, REQUIREMENTS
    from live_clipper.setup_service import METRICOOL_NOTE
    from live_clipper.sources import guide

    platforms, posting = text(GUIDE_DIR / "platforms.md"), text(GUIDE_DIR / "posting.md")
    flat = lambda s: re.sub(r"\s+", " ", s.replace("**", ""))  # noqa: E731
    platforms, posting = flat(platforms), flat(posting)
    assert AS_OF == guide.AS_OF and f"on {AS_OF}" in platforms and f"on {AS_OF}" in posting  # dated
    for p in guide.GUIDE.values():  # every way to connect, its button and its fields, by the names on screen
        for r in p["routes"]:
            assert r["title"] in platforms, r["title"]
            if r["method"]:
                assert r["button"] in platforms, r["button"]
    for provider in ("youtube", "tiktok", "instagram"):
        req = REQUIREMENTS[provider]
        assert req["sign_in"]["button"] in posting, req["sign_in"]["button"]
        for m in req["methods"]:
            assert m["button"] in posting, m["button"]
            for f in m["fields"]:
                assert f["label"] in posting, f["label"]
    for label in ("Keys saved, not signed in", "Could not verify", "Sign in again", "Use a different channel"):  # noqa: E501
        assert label in posting, label
    # Metricool: named in the README summary, at the setup question and in the posting guide, as not included
    readme, first_run = flat(text(ROOT / "README.md")), flat(text(GUIDE_DIR / "first-run.md"))
    assert "Metricool" in METRICOOL_NOTE and "not included in this installation" in METRICOOL_NOTE
    assert "Metricool, an outside scheduling service, is not part of Live Clipper" in readme
    assert "Metricool" in first_run and "not part of this installation" in first_run
    assert "It is not available in this installation." in posting
    for word in ("Connected", "Scheduled", "Awaiting confirmation", "Published"):
        assert f"| {word} |" in posting, word
    assert "An API key can never upload." in platforms and "An API key is not enough here." in posting
