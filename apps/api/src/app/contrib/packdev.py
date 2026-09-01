"""X3: scaffold a domain pack, and doctor any pack against the integrity contract.

`new_pack` writes a droppable module whose template carries the citation contract
where the dev is already looking. `doctor` is the contribution gate for packs:
it lints the SPEC and the citations, then dry-runs the ENTIRE governed loop —
because a pack that cannot reach a minted receipt is not a pack yet.
"""

from __future__ import annotations

from pathlib import Path

RETRIEVAL_GRADES = ("pulled-at-pack-time", "computed-at-pack-time",
                    "archived-document", "config")

_TEMPLATE = '''"""{pack_id}: a contributed domain pack.

Fill in gather(): return (citations, gaps, stats) for one target. The rules the
doctor enforces (and the groundedness gate relies on):
- citations are numbered 1..n with no gaps or duplicates
- every citation carries: n, kind, source, title, text, validation, retrieval
- EVERY NUMBER a drafter may state must appear LITERALLY in a citation's text —
  the gate scans text, so a number that lives only in a series is unstatable
- declare what is missing in `gaps` (strings); an empty list is allowed and the
  platform will still add an explicit no-gaps citation at assembly
- raise ValueError to refuse a target you do not serve (a governed decline)
"""

SPEC = {{
    "id": "{pack_id}",
    "display_name": "{display_name}",
    "version": "v0",
    "target_keys": ({target_keys},),
    "target_doc": {{{target_doc}}},
    "sections": lambda: ["## What the numbers show", "## Method and validation",
                         "## What's missing and how to weigh it"],
    "corpus": None,
    "default_focus": lambda t: " ".join(str(v) for v in t.values()),
    "gather": None,          # set below — late so SPEC reads first
    "doctor_target": {{{doctor_target}}},   # a target gather() can serve offline
}}


def gather(target, focus, trace, extras):
    """TODO: replace the demo evidence with real gathering for {pack_id}."""
    trace.append(f"{pack_id}[{{target}}]")
    citations = [{{
        "n": 1, "kind": "measurement", "source": "TODO: who produced this",
        "title": "demo reading", "validation": "unvalidated",
        "retrieval": "computed-at-pack-time",
        "text": f"Demo reading for {{target}}: 42 units.",
    }}]
    gaps = ["this pack is a scaffold — every real source is missing"]
    return citations, gaps, {{"queries": None}}


SPEC["gather"] = gather
'''


def new_pack(pack_id: str, display_name: str | None = None,
             target_keys: tuple = ("place",), dest_dir: Path | None = None) -> dict:
    """Write app/packs_ext/<id>.py from the template. Refuses to overwrite."""
    safe = pack_id.replace("-", "_")
    if not safe.replace("_", "").isalnum():
        return {"status": "declined",
                "failures": [f"pack id {pack_id!r} must be a simple identifier"]}
    base = Path(dest_dir) if dest_dir else Path(__file__).resolve().parents[1] / "packs_ext"
    dest = base / f"{safe}.py"
    if dest.exists():
        return {"status": "declined",
                "failures": [f"{dest.name} already exists — packs are added, not "
                             "overwritten"]}
    body = _TEMPLATE.format(
        pack_id=pack_id,
        display_name=display_name or pack_id.replace("-", " ").title(),
        target_keys=", ".join(f'"{k}"' for k in target_keys),
        target_doc=", ".join(f'"{k}": "TODO: describe {k}"' for k in target_keys),
        doctor_target=", ".join(f'"{k}": "demo-{k}"' for k in target_keys))
    dest.write_text(body)
    return {"status": "scaffolded", "module": str(dest),
            "next": [f"edit {dest.name}: implement gather()",
                     f"validate: uv run python -m app.contrib.cli doctor {pack_id}",
                     "restart the server to register it"]}


def _lint_citations(citations: list) -> list[str]:
    fails = []
    ns = [c.get("n") for c in citations]
    if ns != list(range(1, len(ns) + 1)):
        fails.append(f"citation numbering must be 1..n with no gaps/duplicates, "
                     f"got {ns}")
    for c in citations:
        label = f"citation [{c.get('n')}]"
        for k in ("kind", "source", "title", "text", "validation"):
            if not str(c.get(k) or "").strip():
                fails.append(f"{label}: missing {k!r} — an evidence entry a reader "
                             "cannot attribute or quote is not evidence")
        r = c.get("retrieval")
        if r and r not in RETRIEVAL_GRADES:
            fails.append(f"{label}: retrieval {r!r} is not one of {RETRIEVAL_GRADES}")
    return fails


def doctor(pack_id: str) -> dict:
    """Lint + full loop dry-run for one registered pack. All failures at once."""
    from ..mcp import assemble, packs, publish

    report: dict = {"pack": pack_id, "checks": [], "failures": []}

    def check(name, ok, detail=""):
        report["checks"].append({"check": name, "ok": bool(ok),
                                 **({"detail": detail} if detail else {})})
        if not ok:
            report["failures"].append(f"{name}: {detail or 'failed'}")

    if pack_id in packs.EXT_ERRORS:
        check("module loads", False, packs.EXT_ERRORS[pack_id])
    if pack_id not in packs.PACKS:
        check("registered", False,
              f"not in PACKS — available: {', '.join(packs.available())}"
              + (f"; load errors: {packs.EXT_ERRORS}" if packs.EXT_ERRORS else ""))
        return {**report, "status": "failed"}
    spec = packs.PACKS[pack_id]

    for k in ("display_name", "version", "target_keys", "target_doc",
              "gather", "sections", "default_focus"):
        ok = spec.get(k) is not None
        check(f"SPEC.{k}", ok, "" if ok else "missing")
    sections = list(spec["sections"]()) if spec.get("sections") else []
    check("sections are markdown h2", sections and
          all(str(x).startswith("## ") for x in sections),
          f"got {sections!r}")

    target = spec.get("doctor_target")
    if not target:
        check("doctor_target", False,
              "SPEC.doctor_target is required: a target gather() can serve so the "
              "doctor can dry-run the loop")
        return {**report, "status": "failed"}

    trace: list = []
    try:
        citations, gaps, stats = spec["gather"](
            dict(target), spec["default_focus"](dict(target)), trace, {})
        check("gather runs", True)
    except Exception as exc:
        check("gather runs", False, f"{type(exc).__name__}: {exc}")
        return {**report, "status": "failed"}
    for f in _lint_citations(citations):
        check("citation contract", False, f)
    if not report["failures"]:
        check("citation contract", True, f"{len(citations)} citations lint clean")
    check("gaps is a list", isinstance(gaps, list), f"got {type(gaps).__name__}")

    # The loop dry-run: a pack that cannot mint a receipt is not done.
    out = assemble.assemble(pack=pack_id, **{k: str(v) for k, v in target.items()})
    check("assemble", out.get("status") == "ok", out.get("note", ""))
    if out.get("status") == "ok":
        check("gaps entry auto-appended",
              out["citations"][-1].get("kind") == "gaps")
        draft = "\n\n".join(f"{s}\n\nStated in the evidence [1]."
                            for s in out["required_sections"])
        pub = publish.answer(out["pack_id"], draft, question="doctor dry-run")
        check("gate + receipt", pub.get("status") == "ok" and pub.get("receipt_id"),
              "; ".join(pub.get("failures", [])) or pub.get("note", ""))

    return {**report, "status": "passed" if not report["failures"] else "failed"}
