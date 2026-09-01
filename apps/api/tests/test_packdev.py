"""X3: pack scaffold + doctor — the contribution gate for whole domain packs."""

import pytest

from app.contrib import packdev
from app.mcp import packs


def _spec(gather=None, **over):
    def default_gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "measurement", "source": "s", "title": "t",
                  "text": "Reading: 42 units.", "validation": "unvalidated",
                  "retrieval": "computed-at-pack-time"}],
                ["no corpus"], {"queries": None})
    d = {"display_name": "Doctor Toy", "version": "v0",
         "target_keys": ("place",), "target_doc": {"place": "site"},
         "gather": gather or default_gather,
         "sections": lambda: ["## Reading", "## Caveats"],
         "corpus": None, "default_focus": lambda t: t.get("place", ""),
         "doctor_target": {"place": "siteA"}}
    d.update(over)
    return d


def test_scaffold_writes_module_and_refuses_overwrite(tmp_path, log):
    out = packdev.new_pack("coastal-tides", dest_dir=tmp_path)
    log("OUTPUT", out["status"])
    assert out["status"] == "scaffolded"
    body = (tmp_path / "coastal_tides.py").read_text()
    assert "SPEC" in body and "LITERALLY in a citation's text" in body
    again = packdev.new_pack("coastal-tides", dest_dir=tmp_path)
    assert again["status"] == "declined"
    assert "not overwritten" in again["failures"][0]


def test_scaffolded_module_passes_doctor_out_of_the_box(tmp_path, monkeypatch, log):
    """The training promise: new-pack -> doctor PASSED with zero edits."""
    packdev.new_pack("fresh", dest_dir=tmp_path)
    ns: dict = {}
    exec((tmp_path / "fresh.py").read_text(), ns)
    monkeypatch.setitem(packs.PACKS, "fresh",
                        {k: v for k, v in ns["SPEC"].items() if k != "id"})
    out = packdev.doctor("fresh")
    log("OUTPUT", f"{out['status']} failures={out['failures']}")
    assert out["status"] == "passed"


def test_doctor_catches_seeded_violations(monkeypatch, log):
    def bad_gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "m", "source": "s", "title": "t",
                  "text": "ok", "validation": "unvalidated",
                  "retrieval": "computed-at-pack-time"},
                 {"n": 3, "kind": "m", "source": "s", "title": "t2",
                  "text": "", "retrieval": "vibes"}],
                ["gap"], {})
    monkeypatch.setitem(packs.PACKS, "sick", _spec(gather=bad_gather))
    out = packdev.doctor("sick")
    blob = "; ".join(out["failures"])
    log("OUTPUT", blob[:200])
    assert out["status"] == "failed"
    assert "numbering must be 1..n" in blob            # duplicate/gapped n
    assert "missing 'text'" in blob                    # nothing quotable
    assert "missing 'validation'" in blob
    assert "'vibes' is not one of" in blob             # bad retrieval grade


def test_doctor_requires_a_dry_runnable_target(monkeypatch, log):
    monkeypatch.setitem(packs.PACKS, "untestable", _spec(doctor_target=None))
    out = packdev.doctor("untestable")
    log("OUTPUT", out["failures"][0][:90])
    assert out["status"] == "failed"
    assert "doctor_target" in out["failures"][0]


def test_doctor_names_unknown_packs_and_load_errors(monkeypatch, log):
    monkeypatch.setitem(packs.EXT_ERRORS, "broke", "SyntaxError: boom")
    out = packdev.doctor("nope")
    log("OUTPUT", out["failures"][0][:100])
    assert out["status"] == "failed" and "not in PACKS" in out["failures"][0]
    out2 = packdev.doctor("broke")
    assert any("SyntaxError: boom" in f for f in out2["failures"])


def test_ext_loader_records_errors_instead_of_crashing(tmp_path, monkeypatch, log):
    """The real loader against a scratch package dir: a SPEC-less module, a
    syntax error, and a collision all record errors; a good module registers."""
    (tmp_path / "no_spec.py").write_text("x = 1\n")
    (tmp_path / "broken.py").write_text("def (\n")
    (tmp_path / "shadow.py").write_text("SPEC = {'id': 'risk'}\n")
    (tmp_path / "good.py").write_text(
        "SPEC = {'id': 'goodpack', 'display_name': 'G', 'version': 'v0',\n"
        "        'target_keys': ('place',), 'target_doc': {'place': 'p'},\n"
        "        'gather': lambda t, f, tr, e: ([], [], {}),\n"
        "        'sections': lambda: ['## A'], 'corpus': None,\n"
        "        'default_focus': lambda t: ''}\n")
    import app.packs_ext as ext
    monkeypatch.setattr(ext, "__path__", [str(tmp_path)])
    monkeypatch.setattr(packs, "EXT_ERRORS", {})
    saved = dict(packs.PACKS)
    try:
        packs._load_ext_packs()
        log("OUTPUT", str(packs.EXT_ERRORS))
        assert packs.EXT_ERRORS["no_spec"] == "module has no SPEC dict"
        assert "SyntaxError" in packs.EXT_ERRORS["broken"]
        assert "cannot shadow" in packs.EXT_ERRORS["shadow"]
        assert "goodpack" in packs.PACKS
        assert packs.PACKS["risk"] is saved["risk"]          # untouched by shadow
    finally:
        packs.PACKS.clear()
        packs.PACKS.update(saved)
