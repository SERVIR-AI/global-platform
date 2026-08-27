"""Platform SKILLS served as MCP resources — the trace disciplines travel with
the platform, not with our repo.

ARCHITECTURE §1's litmus: a capability with no execution and no state ships as a
SKILL, not a tool. `trace-emit` and `trace-visualize` are exactly that — they
teach a builder's own agent to instrument what it builds and to render the
result. A builder connected over MCP cannot read this repository, so each skill
is served WHOLE: SKILL.md plus its references/ and assets/, concatenated with
explicit file boundaries, because the skill itself declares those files are
everything it needs — serving the front page alone would be an incomplete tool.
"""

from __future__ import annotations

from pathlib import Path

from ..config import _REPO_ROOT

_SKILLS_DIR = _REPO_ROOT / ".claude" / "skills"

# Served skills are an allowlist, not a directory scan: .claude/skills also holds
# repo-internal working discipline (tracker, commits, uat) that would be noise —
# or worse, confusion — for an external builder.
SERVED = ("trace-emit", "trace-visualize")


def available() -> list[str]:
    return [n for n in SERVED if (_SKILLS_DIR / n / "SKILL.md").is_file()]


def bundle(name: str) -> str:
    """One skill as a single self-contained document."""
    root = _SKILLS_DIR / name
    if not (root / "SKILL.md").is_file():
        return (f"# {name}\n\nThis skill is not installed on this deployment — "
                "the server was deployed without its skills directory. Declared "
                "honestly rather than served empty.")
    parts = [f"<!-- servirplatform skill bundle: {name} — SKILL.md plus every "
             "reference and asset it declares it needs -->"]
    files = [root / "SKILL.md"]
    for sub in ("references", "assets"):
        d = root / sub
        if d.is_dir():
            files += sorted(p for p in d.iterdir() if p.is_file())
    for f in files:
        rel = f.relative_to(root)
        try:
            body = f.read_text()
        except UnicodeDecodeError:
            continue                      # binary assets do not belong in markdown
        parts.append(f"\n\n===== FILE: {rel} =====\n\n{body}")
    return "".join(parts)
