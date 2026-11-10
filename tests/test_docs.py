"""The docs stay true to the repository.

* ``docs/settings.md`` lists every setting in ``SETTING_SPECS``, and its
  generated tables match what ``scripts/settings_doc.py`` writes now.
* Every link and image in the repository's Markdown resolves: relative paths
  to files that exist, ``#anchors`` to headings that exist, and the README's
  absolute links into this repository (``github.com/.../blob/main/...`` and
  ``raw.githubusercontent.com/.../main/...``, absolute so they also work on
  PyPI) to files in this checkout.
"""

from __future__ import annotations

import importlib.util
import re
import urllib.parse
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "__pycache__", ".pytest_cache",
             ".ruff_cache", "superpowers"}
REPO_URLS = (
    "https://github.com/seangeng/ai-quota-swap/blob/main/",
    "https://github.com/seangeng/ai-quota-swap/tree/main/",
    "https://raw.githubusercontent.com/seangeng/ai-quota-swap/main/",
)


def _settings_doc():
    spec = importlib.util.spec_from_file_location("settings_doc", REPO / "scripts" / "settings_doc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSettingsPage:
    def test_every_setting_is_on_the_page(self):
        from ai_quota_swap.settings import SETTING_SPECS

        text = (REPO / "docs" / "settings.md").read_text(encoding="utf-8")
        missing = [key for key in SETTING_SPECS if f"`{key}`" not in text]
        assert not missing, f"docs/settings.md does not list: {', '.join(missing)}"

    def test_generated_tables_are_current(self):
        doc = _settings_doc()
        text = doc.PAGE.read_text(encoding="utf-8")
        assert doc.current(text) == doc.render(), (
            "docs/settings.md is out of date; run: uv run python scripts/settings_doc.py"
        )

    def test_every_example_command_is_valid_and_changes_something(self):
        from ai_quota_swap.settings import SETTING_SPECS, parse_setting_value

        doc = _settings_doc()
        assert set(doc.MEANINGS) == set(SETTING_SPECS)
        assert set(doc.EXAMPLES) == set(SETTING_SPECS)
        for key, raw in doc.EXAMPLES.items():
            spec = SETTING_SPECS[key]
            assert parse_setting_value(spec, raw) != spec.default, key


# -- links ----------------------------------------------------------------------


def _markdown_files() -> list[Path]:
    out = []
    for path in REPO.rglob("*.md"):
        rel = path.relative_to(REPO)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        out.append(path)
    return sorted(out)


_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.M | re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MD_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_HTML_ATTR = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""")
_SRCSET = re.compile(r"""\bsrcset\s*=\s*["']([^"']+)["']""")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
_HTML_ID = re.compile(r"""\b(?:id|name)\s*=\s*["']([^"']+)["']""")


def _strip_code(text: str) -> str:
    return _INLINE_CODE.sub("", _FENCE.sub("", text))


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercased, links and code marks
    unwrapped, punctuation dropped (``-`` and ``_`` kept), spaces to ``-``."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    return "".join(c for c in text if c.isalnum() or c in "-_ ").replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    body = _FENCE.sub("", text)
    seen: dict[str, int] = {}
    out = set()
    for _, title in _HEADING.findall(body):
        base = slug(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else f"{base}-{n}")
    out.update(_HTML_ID.findall(body))
    return out


def links(path: Path) -> list[str]:
    text = _strip_code(path.read_text(encoding="utf-8"))
    found = _MD_LINK.findall(text) + _HTML_ATTR.findall(text)
    for value in _SRCSET.findall(text):
        found += [part.strip().split()[0] for part in value.split(",") if part.strip()]
    return found


def _resolve(source: Path, target: str) -> tuple[Path | None, str]:
    """The file a link points to in this checkout (None: outside the repo,
    nothing to check) and its anchor."""
    for prefix in REPO_URLS:
        if target.startswith(prefix):
            rest = target[len(prefix):]
            path, _, anchor = rest.partition("#")
            return REPO / urllib.parse.unquote(path), anchor
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):  # http:, mailto:, ...
        return None, ""
    path, _, anchor = target.partition("#")
    path = path.split("?", 1)[0]
    if not path:
        return source, anchor
    return (source.parent / urllib.parse.unquote(path)).resolve(), anchor


def _broken(path: Path) -> list[str]:
    out = []
    for target in links(path):
        resolved, anchor = _resolve(path, target)
        if resolved is None:
            continue
        if not resolved.exists():
            out.append(f"{target}: no such file")
            continue
        if anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
            out.append(f"{target}: no heading for #{anchor}")
    return out


@pytest.mark.skipif(
    not (REPO / ".git").exists(),
    reason="links point at repository files (screenshots, recordings) an sdist does not ship",
)
@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_links_resolve(path):
    broken = _broken(path)
    assert not broken, f"{path.relative_to(REPO)}:\n  " + "\n  ".join(broken)


class TestLinkChecker:
    """The checker itself: it must catch what it is for."""

    def test_slugs_follow_github(self):
        assert slug("Usage resets") == "usage-resets"
        assert slug("`qswap usage --json`") == "qswap-usage---json"
        assert slug("What Anthropic billed: `--billed`") == "what-anthropic-billed---billed"
        assert slug("Login Items & Extensions") == "login-items--extensions"
        assert slug("Coming from claude-swap") == "coming-from-claude-swap"

    def test_it_finds_broken_links_and_anchors(self, tmp_path):
        (tmp_path / "a.md").write_text("# Title\n\n## Second part\n", encoding="utf-8")
        page = tmp_path / "b.md"
        page.write_text(
            "[ok](a.md#second-part) [bad anchor](a.md#nope) [missing](c.md)\n"
            '<img src="img.png"> [self](#here)\n\n## Here\n\n'
            "```\n[ignored](in-a-code-block.md)\n```\n`[ignored](inline.md)`\n",
            encoding="utf-8",
        )
        broken = _broken(page)
        assert broken == [
            "a.md#nope: no heading for #nope",
            "c.md: no such file",
            "img.png: no such file",
        ]

    def test_readme_absolute_links_point_into_the_checkout(self):
        resolved, anchor = _resolve(REPO / "README.md", REPO_URLS[0] + "docs/resets.md#kinds")
        assert resolved == REPO / "docs" / "resets.md" and anchor == "kinds"
        resolved, _ = _resolve(REPO / "README.md", "https://example.com/x.md")
        assert resolved is None
