"""Repository hygiene: secrets never tracked, runtime folders ignored only at the root
(an unanchored `data/` once hid infrastructure/data/ from git)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_anchors_runtime_dirs_and_ignores_env():
    lines = [x.strip() for x in (ROOT / ".gitignore").read_text().splitlines()]
    for d in ("data", "logs", "backups", "trader"):
        assert f"{d}/" not in lines, f"unanchored '{d}/' would hide package folders named {d}"
        assert f"/{d}/" in lines
    assert ".env" in lines and "!.env.example" in lines


def test_no_secrets_in_tracked_text():
    pat = re.compile(r"(DELTA_API_SECRET|DELTA_API_KEY)\s*=\s*[A-Za-z0-9]{12,}")
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".md", ".yaml", ".json", ".example", ".toml"} \
                and ".git" not in p.parts:
            assert not pat.search(p.read_text(errors="ignore")), p


def test_every_skill_has_frontmatter_and_runs_main():
    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    assert len(skills) >= 10
    for s in skills:
        text = s.read_text()
        assert text.startswith("---\nname: ") and "description:" in text
        assert "python3 -m main" in text
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["skills"] == "./skills/"


def test_skill_commands_exist_in_cli():
    from layers.automation.orchestrator import build_parser
    sub = next(a for a in build_parser()._actions if a.__class__.__name__ == "_SubParsersAction")
    cmds = set(sub.choices)
    for s in (ROOT / "skills").glob("*/SKILL.md"):
        for m in re.finditer(r"python3 -m main ([a-z\-]+)", s.read_text()):
            assert m.group(1) in cmds, (s, m.group(1))
