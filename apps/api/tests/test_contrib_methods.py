"""Logic contributed like data: the method kind. A safe calculator, the gate's checks
(the contributor's own worked examples re-run), and every pack computing approved
methods at the assemble seam, cited so the gate can check the numbers."""

import time

import pytest

from app.config import get_settings
from app.contrib import methods, staging
from app.mcp import methods_step, packs, registry, store

UNITS_CSV = ("Unit,Lon,Lat,PctRestorable,RestorableHa,Class\n"
             "OpenCoast_61060,104.88,-2.10,99,343,OpenCoast\n"
             "Estuary_2809,105.90,-3.39,98,1528,Estuary\n"
             "Estuary_3207,105.00,-2.00,52,492,Estuary\n"
             "OpenCoast_61235,107.50,-2.00,95,1000,OpenCoast\n"
             "Delta_1,103.00,-5.00,85,200,Delta\n")
BOX = {"type": "Polygon", "coordinates": [[[104, -4], [106, -4], [106, -1], [104, -1], [104, -4]]]}
EXAMPLE_ROWS = [{"pct_restorable": 99, "restorable_ha": 343},
                {"pct_restorable": 52, "restorable_ha": 492},
                {"pct_restorable": 80, "restorable_ha": 9099}]


def _threshold(**over):
    m = {"method": "restorable_above_threshold",
         "title": "Restorable mangrove above a threshold",
         "description": "Counts units above a restorability share and totals their hectares.",
         "packs": ["land-cover", "risk"],
         "inputs": {"units": {"table": "test_restoration_units"}},
         "params": {"threshold": {"value": 70, "unit": "percent"}},
         "steps": {"selected": "units.pct_restorable > threshold",
                   "n_units": "count(selected)",
                   "restorable_ha": "sum(units.restorable_ha, where=selected)"},
         "outputs": {"n_units": {"unit": "units"}, "restorable_ha": {"unit": "ha"}},
         "source": "test fixture", "license": "CC0-1.0",
         "examples": [{"rows": {"units": EXAMPLE_ROWS},
                       "expect": {"n_units": 2, "restorable_ha": 9442}}]}
    m.update(over)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "feeds_conf_dir", tmp_path / "feeds")
    (tmp_path / "feeds").mkdir()
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    for g in ("STAGED_FEEDS", "STAGED_METHODS", "STAGED_WEIGHTS", "STAGED_AMENDS"):
        monkeypatch.setattr(staging, g, {}, raising=False)
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    registry.reload_declarative_feeds()
    yield tmp_path
    monkeypatch.undo()
    registry.reload_declarative_feeds()


def _contribute_table(dataset, csv, columns, pack="land-cover"):
    out = staging.submit("table", {
        "dataset": dataset, "title": f"Test table {dataset}",
        "description": "A synthetic table for the method tests.", "source": "test fixture",
        "validation": "unvalidated", "license": "CC0-1.0", "vintage": "2026",
        "cadence": "irregular", "units": "mixed", "pack": pack,
        "columns": columns, "csv_text": csv})
    assert out["status"] in ("approved", "staged"), out
    registry.reload_declarative_feeds()
    return out


def _units(env):
    return _contribute_table("test_restoration_units", UNITS_CSV, {
        "unit": "Unit", "lon": "Lon", "lat": "Lat", "pct_restorable": "PctRestorable",
        "restorable_ha": "RestorableHa", "class": "Class"})


def _seam(pack, target, stats):
    citations, gaps, trace = [{"n": 1, "kind": "area", "text": "the area"}], [], []
    block = methods_step.attach(pack, target, stats, citations, gaps, trace, {})
    return block, citations, gaps, trace


# --------------------------------------------------------------------------- the calculator

def test_threshold_count_and_total(log):
    """The worked example of the skill: two units above 70%, 9,442 ha between them."""
    out = methods.evaluate(_threshold(), {"units": EXAMPLE_ROWS})
    log("OUTPUT", out)
    assert out == {"n_units": 2.0, "restorable_ha": 9442.0}


def test_functions_text_and_empty_values(log):
    """Powers and logs (allometry), where/if, text comparisons, lists, weighted means,
    a column named with spaces, and empty values skipped by aggregates."""
    m = {"inputs": {"t": {"table": "x"}},
         "params": {"a": {"value": 0.25, "unit": "1"}, "b": {"value": 2.5, "unit": "1"}},
         "steps": {"agb": "a * t.dbh ** b",
                   "total": "sum(agb)",
                   "logs": "sum(log10(t.dbh))",
                   "big": "count(t.dbh >= 20 and t.kind == 'Rhizophora')",
                   "kinds": "count(t.kind in ['Avicennia', 'Sonneratia'])",
                   "capped": "sum(where(t.dbh > 25, 25, t.dbh))",
                   "tall": "sum(1 if t.dbh > 20 else 0)",
                   "wmean": "weighted_mean(t.dbh, t.weight)",
                   "odd": "sum(field(t, 'Plot Area m2'))",
                   "rows": "count(t)"},
         "outputs": {k: {"unit": "1"} for k in ("total", "logs", "big", "kinds", "capped",
                                                "tall", "wmean", "odd", "rows")}}
    rows = [{"dbh": 10, "kind": "Avicennia", "weight": 1, "Plot Area m2": 100},
            {"dbh": 30, "kind": "Rhizophora", "weight": 3, "Plot Area m2": ""},
            {"dbh": None, "kind": "Sonneratia", "weight": 1, "Plot Area m2": 50}]
    out = methods.evaluate(m, {"t": rows})
    log("OUTPUT", out)
    assert out["total"] == pytest.approx(0.25 * 10 ** 2.5 + 0.25 * 30 ** 2.5)
    assert out["logs"] == pytest.approx(1 + 1.4771212547)
    assert out["big"] == 1 and out["kinds"] == 2 and out["tall"] == 1 and out["rows"] == 3
    assert out["capped"] == 35
    assert out["wmean"] == pytest.approx((10 * 1 + 30 * 3) / 4)
    assert out["odd"] == 150


@pytest.mark.parametrize("expr, said", [
    ("__import__('os').system('touch /tmp/grp_method_pwned')", "unknown function"),
    ("units.__class__", "not a field"),
    ("(lambda: 1)()", "unknown function"),
    ("[x for x in units.pct_restorable]", "not allowed"),
    ("units.pct_restorable[0]", "not allowed"),
    ("f'{threshold}'", "not allowed"),
    ("open('/etc/passwd')", "unknown function"),
    ("units.pct_restorable.real", "dotted name must be"),
    ("threshold.__add__(1)", "unknown function"),
    ("sum(units.restorable_ha, start=5)", "only where="),
    ("count(threshold)", "counts rows"),
    ("units.class > 'A'", "not ordered"),
    ("'a' + 'b'", "text can only be compared"),
    ("nope + 1", "unknown name"),
    ("+".join(["1"] * 400), "longer than"),
    ("abs(" * 40 + "1" + ")" * 40, "too long"),
])
def test_anything_but_the_vocabulary_is_declined(expr, said, log):
    m = _threshold(steps={"x": expr, "n_units": "count(units)", "restorable_ha": "sum(units.restorable_ha)"})
    with pytest.raises(methods.MethodError) as exc:
        methods.compile_steps(m)
    log("OUTPUT", str(exc.value)[:110])
    assert said in str(exc.value)


def test_nothing_in_a_declined_step_ever_runs(tmp_path):
    flag = tmp_path / "pwned"
    m = _threshold(steps={"x": f"__import__('os').system('touch {flag}')"})
    assert methods.validate(m)
    with pytest.raises(methods.MethodError):
        methods.evaluate(m, {"units": EXAMPLE_ROWS})
    assert not flag.exists()


def test_a_huge_power_is_a_bounded_float_not_a_hang(log):
    m = _threshold(steps={"n_units": "count(units)", "restorable_ha": "10 ** 10 ** 10"})
    t0 = time.perf_counter()
    with pytest.raises(methods.MethodError) as exc:
        methods.evaluate(m, {"units": EXAMPLE_ROWS})
    log("OUTPUT", f"{time.perf_counter() - t0:.4f}s {exc.value}")
    assert time.perf_counter() - t0 < 1.0 and "not a number" in str(exc.value)


def test_rows_of_two_inputs_never_line_up():
    m = _threshold(inputs={"units": {"table": "a"}, "plots": {"table": "b"}},
                   steps={"x": "units.restorable_ha + plots.area", "n_units": "count(units)",
                          "restorable_ha": "sum(units.restorable_ha)"})
    with pytest.raises(methods.MethodError) as exc:
        methods.compile_steps(m)
    assert "mixes the rows" in str(exc.value)


# --------------------------------------------------------------------------- the gate, offline

def test_missing_fields_are_named_and_attested_ones_asked_for(log):
    problems = methods.validate({})
    log("OUTPUT", problems[:3])
    for k in ("method", "steps", "outputs", "inputs", "packs"):
        assert any(f"'{k}'" in p for p in problems)
    for k in ("source", "license", "title", "description", "examples"):
        assert any(f"'{k}'" in p and "ASK the contributor" in p for p in problems)


def test_an_unknown_pack_lists_every_pack_from_the_registry():
    problems = methods.validate(_threshold(packs=["landcover"]))
    said = next(p for p in problems if "unknown pack" in p)
    assert all(p in said for p in packs.PACKS)


def test_the_worked_example_is_rerun_and_a_mismatch_declined(log):
    m = _threshold()
    m["examples"][0]["expect"]["restorable_ha"] = 9000
    problems = methods.validate(m)
    log("OUTPUT", problems)
    assert any("example 1: restorable_ha came out 9,442" in p and "expects 9,000" in p
               for p in problems)


def test_an_output_is_one_number_with_a_unit():
    problems = methods.validate(_threshold(outputs={"selected": {"unit": "1"},
                                                    "n_units": {}}))
    assert any("one value per row" in p for p in problems)
    assert any("needs {unit" in p for p in problems)


def test_a_parameter_needs_a_unit():
    problems = methods.validate(_threshold(params={"threshold": {"value": 70}}))
    assert any("has no unit" in p for p in problems)


# --------------------------------------------------------------------------- through the gate

def test_a_method_on_a_freshly_contributed_table_lands_and_is_listed(env, log):
    _units(env)
    out = staging.submit("method", _threshold())
    log("OUTPUT", {k: out.get(k) for k in ("status", "contribution_id", "review")})
    assert out["status"] in ("approved", "staged"), out
    assert "restorable_above_threshold" in methods.for_pack("land-cover")
    assert "restorable_above_threshold" in methods.for_pack("risk")
    assert "restorable_above_threshold" not in methods.for_pack("food-security")
    assert "restorable_above_threshold" in registry._methods_listed()
    if out["status"] == "approved":
        assert (get_settings().methods_conf_dir / "restorable_above_threshold.yml").is_file()


def test_unknown_inputs_and_fields_are_declined_with_the_closest_names(env, log):
    _units(env)
    bad_ds = staging.submit("method", _threshold(inputs={"units": {"table": "test_restoration_unit"}}))
    log("OUTPUT", bad_ds["problems"])
    assert bad_ds["status"] == "declined" and "test_restoration_units" in bad_ds["problems"][0]
    m = _threshold(method="typo_field", steps={
        "selected": "units.pct_restorabl > threshold", "n_units": "count(selected)",
        "restorable_ha": "sum(units.restorable_ha, where=selected)"})
    m["examples"][0]["rows"]["units"] = [{"pct_restorabl": 99, "restorable_ha": 343}]
    m["examples"][0]["expect"] = {"n_units": 1, "restorable_ha": 343}
    bad_field = staging.submit("method", m)
    log("OUTPUT", bad_field["problems"])
    assert bad_field["status"] == "declined" and "pct_restorable" in bad_field["problems"][0]


def test_a_name_is_never_overwritten(env):
    _units(env)
    assert staging.submit("method", _threshold())["status"] in ("approved", "staged")
    again = staging.submit("method", _threshold())
    assert again["status"] == "declined" and "already" in again["problems"][0]


# --------------------------------------------------------------------------- every pack, at the seam

def test_every_pack_computes_it_over_its_own_area(env, log):
    _units(env)
    staging.submit("method", _threshold())
    area = {"name": "Test Province", "geometry": BOX, "countries": ["Indonesia"]}
    for pack, target in (("land-cover", {"place": "Test Province", "year": "2020"}),
                         ("risk", {"place": "Test Province", "hazard": "flood"})):
        block, cits, gaps, trace = _seam(pack, target, {"_area": area})
        log("OUTPUT", f"{pack}: {cits[-1]['text'][:160]}")
        cit = cits[-1]
        assert cit["n"] == 2 and cit["kind"] == "method_result"
        assert cit["results"]["n_units"]["value"] == 2
        assert cit["results"]["restorable_ha"]["value"] == 1871
        assert "1,871 ha" in cit["text"] and "3 of its 5 row(s)" in cit["text"]
        assert block["restorable_above_threshold"]["inputs"]["units"]["rows_about_area"] == 3
        assert not gaps


def test_food_security_places_rows_by_country(env, log):
    _contribute_table("test_plot_yields", "Country,Yield\nKenya,2.1\nKenya,1.9\nZambia,3.0\n",
                      {"country": "Country", "yield_t_ha": "Yield"}, pack="food-security")
    m = {"method": "mean_plot_yield", "title": "Mean plot yield", "description": "d",
         "packs": ["food-security"], "inputs": {"plots": {"table": "test_plot_yields"}},
         "steps": {"mean_yield": "mean(plots.yield_t_ha)"},
         "outputs": {"mean_yield": {"unit": "t/ha"}}, "source": "test fixture",
         "license": "CC0-1.0",
         "examples": [{"rows": {"plots": [{"yield_t_ha": 1}, {"yield_t_ha": 3}]},
                       "expect": {"mean_yield": 2}}]}
    assert staging.submit("method", m)["status"] in ("approved", "staged")
    block, cits, gaps, trace = _seam("food-security", {"country": "Kenya", "crop": "maize"}, {})
    log("OUTPUT", cits[-1]["text"][:160])
    assert cits[-1]["results"]["mean_yield"]["value"] == pytest.approx(2.0)
    assert "their country is Kenya" in cits[-1]["text"]


def test_a_method_that_holds_elsewhere_is_not_run_here(env, log):
    _units(env)
    staging.submit("method", _threshold(countries=["Ecuador"]))
    block, cits, gaps, trace = _seam("land-cover", {"place": "Test Province", "year": "2020"},
                                     {"_area": {"name": "Test Province", "geometry": BOX,
                                                "countries": ["Indonesia"]}})
    log("OUTPUT", trace)
    assert block is None and len(cits) == 1
    assert any("holds for Ecuador" in t for t in trace)


def test_the_gate_sees_the_platforms_numbers(env):
    from app.food_security.synthesis import check_grounded
    _units(env)
    staging.submit("method", _threshold())
    _, cits, _, _ = _seam("land-cover", {"place": "Test Province", "year": "2020"},
                          {"_area": {"name": "Test Province", "geometry": BOX}})
    draft = "## Answer\nThe platform counted 2 units holding 1,871 ha of restorable mangrove [2].\n"
    verdict = check_grounded(draft, cits, sections=("## Answer",))
    assert not verdict["numbers_unverified"], verdict


def test_withdrawing_takes_it_out_of_every_pack(env, log):
    _units(env)
    out = staging.submit("method", _threshold())
    gone = staging.withdraw(out["contribution_id"])
    log("OUTPUT", gone)
    assert "restorable_above_threshold" not in methods.for_pack("land-cover")
    block, cits, _, _ = _seam("land-cover", {"place": "Test Province", "year": "2020"},
                              {"_area": {"name": "Test Province", "geometry": BOX}})
    assert block is None and len(cits) == 1
    if out["status"] == "approved":
        kept = list(get_settings().methods_conf_dir.glob("restorable_above_threshold.yml.retired-*"))
        assert kept, "the withdrawn method's file is kept for replay"


def test_assemble_runs_methods_for_the_pack_it_builds(env, monkeypatch, log):
    from app.mcp import assemble
    _units(env)
    staging.submit("method", _threshold())

    def gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "area", "retrieval": "computed-at-pack-time",
                  "source": "test", "title": "the area", "text": "Test Province"}], [],
                {"queries": None, "_area": {"name": "Test Province", "geometry": BOX}})
    monkeypatch.setitem(packs.PACKS["land-cover"], "gather", gather)
    out = assemble.assemble(pack="land-cover", place="Test Province", year="2020")
    log("OUTPUT", {k: out.get(k) for k in ("status", "methods")})
    assert out["status"] == "ok"
    assert out["methods"]["restorable_above_threshold"]["results"]["restorable_ha"]["value"] == 1871
    assert any(c.get("kind") == "method_result" for c in out["citations"])


# --------------------------------------------------------------------------- the skill

def test_the_skill_travels_with_the_platform():
    from app.mcp import skills
    assert "contribute-logic" in skills.available()
    text = skills.bundle("contribute-logic")
    assert "Contribute logic as a method" in text and "Their words, not yours" in text
    guide = staging.describe_submit()
    assert "method — how:" in guide and methods.SKILL in guide


# --------------------------------------------------------------------------- finding them

def test_the_map_lists_methods_first_and_stays_readable(env, log):
    """The listing buried methods past 125 KB of feed rows, where no client read them."""
    import json
    _units(env)
    staging.submit("method", _threshold())
    caps = registry.capabilities(available_tools=["platform_capabilities"])
    keys = list(caps)
    assert keys.index("methods") < keys.index("tools_available") < keys.index("feeds")
    assert "restorable_above_threshold" in caps["methods"] and "not tools" in caps["methods_note"]
    assert "never copy its steps into your code" in caps["methods_note"]
    assert "accept or reject first" in caps["methods_note"]
    assert "test_restoration_units" in caps["feeds"] or "test_restoration_units" in caps["staged_feeds"]
    row = caps["feeds"].get("test_restoration_units")
    if row is not None:
        assert {"pct_restorable", "restorable_ha"} <= set(row["fields"])
    assert all("fetch" not in row and len(json.dumps(row)) < 1500 for row in caps["feeds"].values())
    for p in caps["domain_packs"]:
        if "sources" in p:
            assert set(p["sources"]) == {"count", "full_list"} and p["sources"]["full_list"].endswith(p["id"])
    log("OUTPUT", f"{len(json.dumps(caps)):,} chars, {len(caps['feeds'])} feeds")


def test_the_instructions_say_what_a_method_is():
    from app.mcp.server import INSTRUCTIONS
    line = next(l for l in INSTRUCTIONS.split("\n- ") if l.startswith("METHODS"))
    assert "not these tools" in line and "platform_capabilities lists them first" in line


def test_a_method_can_be_previewed_and_nothing_is_stored(env, log):
    """Claude tried preview first in every UAT run; it now checks everything and stores nothing."""
    _units(env)
    out = staging.submit("method", {**_threshold(method="preview_only_method"), "preview": True})
    log("OUTPUT", {k: out.get(k) for k in ("status", "next")})
    assert out["status"] == "preview" and out["would_land"]["method"] == "preview_only_method"
    assert "the worked examples, re-run" in out["would_land"]["checked"]
    assert "preview_only_method" not in methods.for_pack("land-cover")
    assert not (get_settings().methods_conf_dir / "preview_only_method.yml").exists()


def test_the_dry_run_hands_out_no_figures(env):
    """A dry run over rows placed in no area answers nothing, so it gives no numbers to quote."""
    _units(env)
    out = staging.submit("method", _threshold())
    sample = out["preview"]["sample"]
    assert sample["outputs"] == ["n_units", "restorable_ha"] and "ran" in sample


def test_a_proposed_calculation_comes_without_its_result():
    from app.mcp import loop
    from app.mcp.server import INSTRUCTIONS
    assert "without results" in INSTRUCTIONS and "without their results" in loop.ENTRY_HINT["no_arithmetic"]
