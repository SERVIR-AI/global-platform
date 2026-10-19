"""Sprint dry run (2026-10-19): land-cover answers on contributed stacks, tables and
documents, and the risk pack asked about a place outside its maps.

One-class extent maps state areas and a series, never loss and gain; the default
stack maps two or more classes; factor tables apply only where they are scoped and
match class names as whole names (aliases, plurals); context tables and feeds are
cited by their rows inside the area; documents about another country are not cited;
an area with no stack still measures the vectors asked; each year cites its own
accuracy. All on synthetic data, never presented as real."""
import json
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.graph.geo import ingest
from app.land_cover import synthesis as lc
from app.mcp import assemble, feeds, packs, registry, store, vectors as vectors_step
from app.risk import synthesis as risk

W = H = 10
PX = 0.001
LEFT, TOP = -85.0, 10.0
POLY = mapping(box(LEFT + 1 * PX, TOP - 9 * PX, LEFT + 9 * PX, TOP - 1 * PX))   # columns/rows 1-8
FAR_AWAY = [39.80, -3.90, 39.85, -3.85]                                        # no stack reaches it
OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")


def _write(path, arr, nodata=None, left=LEFT, top=TOP):
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                       dtype=arr.dtype, crs="EPSG:4326", transform=from_origin(left, top, PX, PX),
                       nodata=nodata) as dst:
        dst.write(arr, 1)
    return str(path)


def _stack(title, legend, outside, pattern, years, data, accuracy="unstated", countries=("Testland",),
           extent=None):
    return {"kind": "stack", "role": "landcover", "internal": True, "title": title,
            "description": "synthetic", "source": "test fixture", "license": "CC0-1.0",
            "pub_date": "2026-01-01", "authority": "test", "accuracy": accuracy, "legend": legend,
            "outside_values": outside, "years": years, "countries": list(countries),
            "extent": extent or {"lon": [LEFT, LEFT + W * PX], "lat": [TOP - H * PX, TOP]},
            "layer_pattern": f"landcover_{pattern}_{{year}}", "file_pattern": f"{pattern}-{{year}}.tif",
            "data_root": str(data)}


def _table(feeds_dir, cache, dataset, csv_text, columns, **extra):
    """A landed table bound to the land-cover pack, written the way the gate lands one."""
    (cache / "tables").mkdir(parents=True, exist_ok=True)
    (cache / "tables" / f"{dataset}.csv").write_text(csv_text)
    spec = {"dataset": dataset, "title": extra.pop("title", f"{dataset} (synthetic)"),
            "description": "synthetic table", "source": "test fixture", "validation": "unvalidated",
            "residency": "platform-hosted copy", "cadence": "irregular", "adapter": "generic_csv",
            "license": "CC0-1.0", "vintage": "2026-10", "pack": "land-cover", "contributed": True,
            "fetch": {"path": f"tables/{dataset}.csv", "columns": columns, "units": "per hectare"},
            **extra}
    (feeds_dir / f"{dataset}.yml").write_text(json.dumps(spec))
    registry.reload_declarative_feeds()


@pytest.fixture
def lc_env(tmp_path, monkeypatch):
    """Four stacks registered in this order over a 10x10 bay: a one-class extent map
    (0 = outside), the same map with its background as a class, a three-class cover map
    whose 2021 year declares its own accuracy, and a stack on another continent."""
    s = get_settings()
    data = tmp_path / "data"; data.mkdir()
    a = np.zeros((H, W), "uint8"); a[:, :5] = 1                    # 1996: columns 0-4 mangrove
    b = np.zeros((H, W), "uint8"); b[:, 1:3] = 1; b[:, 6] = 1      # 2020: columns 1, 2 and 6
    for y, arr in ((1996, a), (2020, b)):
        _write(data / f"one-{y}.tif", arr, nodata=0)
        _write(data / f"two-{y}.tif", arr)
    cover = np.full((H, W), 10, "uint8"); cover[:, :3] = 95; cover[:, 8:] = 80
    for y in (2020, 2021):
        _write(data / f"wc-{y}.tif", cover, nodata=0)
    cover_legend = {10: "Tree cover", 80: "Permanent water bodies", 95: "Mangroves"}
    cat = {
        "landcover_one": _stack("One-class extent (synthetic)", {1: "Mangrove"}, [0], "one", [1996, 2020],
                                data, accuracy="88% (synthetic)"),
        "landcover_two": _stack("Two-class extent (synthetic)", {0: "Not mangrove", 1: "Mangrove"}, [],
                                "two", [1996, 2020], data),
        "landcover_wc": _stack("Cover map (synthetic)", cover_legend, [0], "wc", [2020, 2021], data,
                               accuracy="74.4% for the v100 map (synthetic)"),
        "landcover_far": _stack("Far stack (synthetic)", {1: "Forest", 2: "Cropland"}, [0], "far", [2020],
                                data, countries=("Farland",), extent={"lon": [85.0, 85.01], "lat": [27.0, 27.01]}),
        # the cover stack's 2021 file is a newer map version, landed with its own accuracy
        "landcover_wc_2021": {"stack": "landcover_wc", "year": 2021, "internal": True,
                              "path": str(data / "wc-2021.tif"), "local_path": "stacks/wc-2021.tif",
                              "title": "Cover map (synthetic) — 2021", "legend": cover_legend,
                              "outside_values": [0], "source": "test fixture", "license": "CC0-1.0",
                              "accuracy": "76.7% for the v200 map (synthetic)"},
        "hazard_toyfar": {"title": "Toy flood (synthetic, another region)", "local_path": "tiffs/hazard_toyfar.tif",
                          "legend": {1: "Low", 2: "High"},
                          "extent": {"lon": [100.0, 101.0], "lat": [10.0, 11.0]}},
    }
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "tiffs.yml").write_text(json.dumps(cat))
    contract = {"role": "landcover", "dtype": "uint8", "valid_min": 0, "valid_max": 95, "pixel_size_deg": PX}
    (conf / "raster_schema.yml").write_text(json.dumps({
        "defaults": {"crs": "EPSG:4326", "float_tol": 0.0001}, "layers": {},
        "stacks": {sid: contract for sid in ("landcover_one", "landcover_two", "landcover_wc", "landcover_far")}}))
    feeds_dir = tmp_path / "feeds"; feeds_dir.mkdir()
    cache = tmp_path / "cache"
    for k, v in (("tiffs_config_path", conf / "tiffs.yml"), ("tiffs_contrib_path", conf / "tiffs.contrib.yml"),
                 ("raster_schema_path", conf / "raster_schema.yml"),
                 ("raster_schema_contrib_path", conf / "raster_schema.contrib.yml"),
                 ("cache_dir", cache), ("tiffs_dir", cache / "tiffs"), ("feeds_conf_dir", feeds_dir),
                 ("grp_mattermost_webhook", ""), ("grp_auto_approve", True)):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    registry.reload_declarative_feeds()
    staging.STAGED_FEEDS.clear()
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    yield {"feeds": feeds_dir, "cache": cache, "tmp": tmp_path}
    monkeypatch.undo()
    registry.reload_declarative_feeds()
    staging.STAGED_FEEDS.clear()
    staging.STAGED_VECTORS.clear()


def _kinds(p):
    return [c["kind"] for c in p["citations"]]


# ------------------------------------------------------- 1. one-class stacks
def test_a_one_class_map_states_areas_and_a_series_never_loss_or_gain(lc_env, log):
    _table(lc_env["feeds"], lc_env["cache"], "soc_test", "class,factor,unit,pool\nMangrove,200,t C/ha,soil carbon\n",
           {"class": "class", "factor": "factor", "unit": "unit", "pool": "pool"})
    p = assemble.assemble(pack="land-cover", place="Test bay", year="1996", year_to="2020",
                          geometry=POLY, stack="landcover_one")
    assert p["status"] == "ok", p.get("note")
    kinds = _kinds(p)
    log("OUTPUT", f"kinds={kinds}")
    assert "class_change" not in kinds and "class_transitions" not in kinds
    assert kinds.count("class_area") == 2 and "change" not in p["stats"]
    series = next(c for c in p["citations"] if c["kind"] == "class_series")["text"]
    # inside columns 1-8: 1996 mangrove = columns 1-4, 2020 = columns 1, 2, 6 -> one column less
    assert "1996:" in series and "2020:" in series and "2020 vs 1996: -" in series
    factor = next(c for c in p["citations"] if c["kind"] == "class_factor")["text"]
    assert "Mangrove 1996:" in factor and "; 2020:" in factor
    assert "loss" not in factor and "gain" not in factor
    entry = p["stats"]["factors"]["soc_test"]["Mangrove"][0]
    assert "value_to" in entry and "loss_ha" not in entry and "value_lost" not in entry
    gaps = " | ".join(p["gaps"])
    assert "maps one class (Mangrove)" in gaps and "cannot be told from a pixel with no data" in gaps
    assert "background as a class" in gaps
    method = next(c for c in p["citations"] if c["kind"] == "method")["text"]
    assert "Change: not computed" in method and "Change: computed only" not in method
    # its values are its class area: not read again as a uniform layer
    assert "layer_values" not in kinds and "holds ONE value" not in gaps


def test_the_two_class_map_of_the_same_extent_still_shows_loss_and_gain(lc_env):
    p = assemble.assemble(pack="land-cover", place="Test bay", year="1996", year_to="2020",
                          geometry=POLY, stack="landcover_two", classes=["Mangrove"])
    assert p["status"] == "ok", p.get("note")
    ch = p["stats"]["change"]["by_class"]["Mangrove"]
    assert ch["loss_km2"] > 0 and ch["gain_km2"] > 0
    assert not any("maps one class" in g for g in p["gaps"])


# ------------------------------------------------------- 2. the default stack
def test_the_default_stack_maps_two_or_more_classes_and_says_why(lc_env, log):
    p = assemble.assemble(pack="land-cover", place="Test bay", year="1996", year_to="2020", geometry=POLY)
    assert p["status"] == "ok", p.get("note")
    assert p["stats"]["stack"] == "landcover_two"          # the one-class map was registered first
    gap = next(g for g in p["gaps"] if "also cover" in g)
    log("OUTPUT", gap[:300])
    assert "One-class extent (synthetic) (landcover_one)" in gap
    assert "two or more classes first" in gap and "maps 2 class(es)" in gap and "pass stack=<id>" in gap


# ------------------------------------------------------- 7. the class grid for the seam
def test_the_end_years_class_map_rides_the_stats_for_the_layer_step(lc_env):
    trace = []
    _c, _g, stats = lc.gather_land_cover_evidence({"place": "Test bay", "year": "1996", "year_to": "2020"},
                                                  "focus", trace, {"geometry": POLY})
    grid = stats["_class_grid"]
    assert grid["year"] == "2020" and grid["stack"] == "landcover_two"
    assert grid["legend"] == {0: "Not mangrove", 1: "Mangrove"} and grid["outside_values"] == []
    assert grid["label"] == "Two-class extent (synthetic)" and os.path.exists(grid["path"])
    with rasterio.open(grid["path"]) as ds:
        arr = ds.read(1)
    assert arr[:, 6].all() and not arr[:, 4].any()         # the 2020 map, not 1996's


# ------------------------------------------------------- 8. accuracy per year
def test_each_year_cites_its_own_declared_accuracy(lc_env, log):
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", year_to="2021",
                          geometry=POLY, stack="landcover_wc")
    assert p["status"] == "ok", p.get("note")
    passports = {c["title"]: c["text"] for c in p["citations"] if c["kind"] == "landcover_layer"}
    assert "74.4%" in passports["Cover map (synthetic), 2020"]
    assert "76.7%" in passports["Cover map (synthetic), 2021"]
    gap = next(g for g in p["gaps"] if g.startswith("accuracy"))
    log("OUTPUT", gap)
    assert "2020: 74.4%" in gap and "2021: 76.7%" in gap
    q = assemble.assemble(pack="land-cover", place="Test bay", year="1996", geometry=POLY, stack="landcover_two")
    gaps = " | ".join(q["gaps"])
    assert "no accuracy figure was declared with the layer" in gaps
    assert "method publication has not been archived" not in gaps


# ------------------------------------------------------- 3. factor tables: scope and labels
def test_a_scoped_factor_table_applies_only_where_it_is_scoped(lc_env, log):
    cols = {"class": "class", "factor": "factor", "unit": "unit", "pool": "pool"}
    _table(lc_env["feeds"], lc_env["cache"], "soc_elsewhere", "class,factor,unit,pool\nMangrove,232.3,Mg C/ha,soil\n",
           cols, title="Elsewhere bay soil carbon factor (synthetic)", countries=["Elsewhere"],
           bbox=[10.0, 10.0, 11.0, 11.0])
    _table(lc_env["feeds"], lc_env["cache"], "soc_here", "class,factor,unit,pool\nMangrove,150,Mg C/ha,soil\n",
           cols, title="Test bay soil carbon factor (synthetic)", countries=["Testland"],
           bbox=[LEFT, TOP - 1, LEFT + 1, TOP])
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=POLY, stack="landcover_wc")
    assert p["status"] == "ok", p.get("note")
    factors = [c for c in p["citations"] if c["kind"] == "class_factor"]
    assert [c["dataset"] for c in factors] == ["soc_here"]
    # 'Mangrove' in the table names the legend's 'Mangroves' (plurals fold)
    assert "Mangroves 2020:" in factors[0]["text"]
    gap = next(g for g in p["gaps"] if "soc_elsewhere" in g)
    log("OUTPUT", gap)
    assert "is scoped to Elsewhere" in gap and "not applied here" in gap
    # the far stack's legend (Forest, Cropland) names no mangrove: no gap there, only the trace
    q = lc._factor_citation(1, "soc_here", {"title": "t", "fetch": {"columns": cols}},
                            {"records": [{"class": "Mangrove", "factor": 150}]}, "Far valley", 2020, None,
                            {1: "Forest", 2: "Snow and glacier"},
                            {"by_class": {1: {"ha": 1.0}, 2: {"ha": 1.0}}}, None, None, gaps := [], [])
    assert q == (None, None) and gaps == []


def test_factor_classes_match_whole_names_with_aliases_never_substrings():
    cols = {"class": "class", "factor": "factor"}
    spec = {"title": "t", "fetch": {"columns": cols}}

    def apply(legend, rows):
        gaps = []
        st = {"by_class": {k: {"ha": 10.0} for k in legend}}
        cit, stats = lc._factor_citation(1, "t", spec, {"records": rows}, "X", 2020, None, legend, st,
                                         None, None, gaps, [])
        return cit, stats, gaps

    cit, stats, _ = apply({1: "Mangrove forest"}, [{"class": "Mangrove|Mangroves|Mangrove forest|Manglar", "factor": 5}])
    assert cit and "Mangrove forest" in stats
    cit, stats, _ = apply({1: "Manglares"}, [{"class": "Mangrove|Manglares", "factor": 5}])
    assert cit and "Manglares" in stats
    # 'Mangrove' never matches 'Non-mangrove', and a negation is never 'plausibly the same'
    cit, _s, gaps = apply({1: "Non-mangrove"}, [{"class": "Mangrove", "factor": 5}])
    assert cit is None and gaps == []
    # a legend that plausibly names the class gets the alias hint
    cit, _s, gaps = apply({1: "Mangrove forest"}, [{"class": "Mangrove", "factor": 5}])
    assert cit is None and "may be the same thing" in gaps[0] and "'Mangrove|Mangrove forest'" in gaps[0]
    assert lc._fold("Mangroves") == lc._fold("mangrove") and lc._fold("Non-mangrove") != lc._fold("Mangrove")


def test_rows_of_one_class_and_pool_are_alternatives_never_one_answer():
    cols = {"class": "class", "factor": "factor", "unit": "unit", "pool": "pool", "climate": "climate"}
    rows = [{"class": "Mangrove", "factor": 192, "unit": "t d.m./ha", "pool": "above-ground biomass", "climate": "Tropical wet"},
            {"class": "Mangrove", "factor": 92, "unit": "t d.m./ha", "pool": "above-ground biomass", "climate": "Tropical dry"},
            {"class": "Mangrove", "factor": 386, "unit": "t C/ha", "pool": "soil carbon", "climate": ""}]
    st = {"by_class": {1: {"ha": 100.0}}}
    cit, stats = lc._factor_citation(1, "ipcc", {"title": "IPCC (synthetic)", "fetch": {"columns": cols}},
                                     {"records": rows}, "X", 2020, None, {1: "Mangrove"}, st, None, None, [], [])
    text = cit["text"]
    assert "the table gives one row per climate; use the one that applies, never their sum" in text
    assert "[climate: Tropical wet]" in text and "[climate: Tropical dry]" in text
    assert "19,200" in text and "9,200" in text and "38,600" in text
    agb = [e for e in stats["Mangrove"] if e.get("pool") == "above-ground biomass"]
    assert {e["alternatives_by"] for e in agb} == {"climate"}
    assert not any("alternatives_by" in e for e in stats["Mangrove"] if e.get("pool") == "soil carbon")


# ------------------------------------------------------- 4. context tables and feeds
def test_context_tables_are_cited_by_their_rows_inside_the_area(lc_env, log):
    inside = (LEFT + 4 * PX, TOP - 4 * PX)
    _table(lc_env["feeds"], lc_env["cache"], "cores_test",
           "core_id,latitude,longitude,habitat,depth_cm\n"
           f"a,{inside[1]},{inside[0]},mangrove,100\nb,{inside[1]},{inside[0] + PX},mangrove,200\n"
           "c,51.5,-0.1,marsh,50\nd,-33.9,151.2,seagrass,30\n",
           {"core_id": "core_id", "latitude": "latitude", "longitude": "longitude", "habitat": "habitat",
            "depth_cm": "depth_cm"}, title="Soil cores, worldwide (synthetic)")
    _table(lc_env["feeds"], lc_env["cache"], "faraway_plots", "plot,lat,lon,height\np1,51.5,-0.1,12\n",
           {"plot": "plot", "lat": "lat", "lon": "lon", "height": "height"}, title="UK plots (synthetic)")
    _table(lc_env["feeds"], lc_env["cache"], "by_country", "country,stock\nTestland,10\nTestland,20\nElsewhere,99\n",
           {"country": "country", "stock": "stock"}, title="Stocks by country (synthetic)")
    _table(lc_env["feeds"], lc_env["cache"], "unplaced", "species,count\nRhizophora,3\n",
           {"species": "species", "count": "count"}, title="Species counts (synthetic)")
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=POLY, stack="landcover_wc")
    assert p["status"] == "ok", p.get("note")
    tables = {c["dataset"]: c for c in p["citations"] if c["kind"] == "table"}
    log("OUTPUT", tables["cores_test"]["text"][:300])
    assert sorted(tables) == ["by_country", "cores_test"]
    cores = tables["cores_test"]["text"]
    assert "2 of its 4 row(s) are about Test bay" in cores
    assert "depth_cm min 100, mean 150, max 200" in cores and "habitat: mangrove (2)" in cores
    assert "marsh" not in cores and "first rows" not in cores
    assert "2 of its 3 row(s)" in tables["by_country"]["text"] and "stock min 10, mean 15, max 20" in tables["by_country"]["text"]
    gaps = " | ".join(p["gaps"])
    assert "unplaced" in gaps and "no coordinate or country column" in gaps
    assert "faraway_plots" not in gaps                     # simply about somewhere else: the trace says so
    assert any("faraway_plots" in t for t in p["trace"])


def test_a_feed_is_labelled_a_feed_and_the_true_row_count_is_stated(log):
    from shapely.geometry import shape
    spec = {"title": "SST per cell (synthetic)", "source": "test", "validation": "peer-reviewed",
            "contributed": True, "fetch": {"url": "https://x.test/sst.json"}}
    recs = [{"lat": TOP - 2 * PX, "lon": LEFT + 2 * PX, "sst": 28.5}, {"lat": TOP - 3 * PX, "lon": LEFT + 3 * PX, "sst": None},
            {"lat": 0.0, "lon": 0.0, "sst": 20.0}]
    res = {"records": recs, "summary": "16,715 records from SST per cell (synthetic)", "as_of": "2010-01-01",
           "passport": {"url": "https://x.test/sst.json"}}
    cit, note, placeable = lc._context_citation(3, "sst_test", spec, res, "Test bay", ["Testland"], shape(POLY))
    log("OUTPUT", cit["text"])
    assert cit["kind"] == "feed" and cit["retrieval"] == "pulled-at-pack-time"
    assert cit["validation"] == "contributed-feed (peer-reviewed)"
    assert "2 of its 16,715 row(s) are about Test bay" in cit["text"]
    assert "Only the last 3 of the 16,715 rows were read" in cit["text"]
    assert "sst min 28.5, mean 28.5, max 28.5 (1 with a value)" in cit["text"]


# ------------------------------------------------------- 5. documents across countries
def test_documents_about_another_country_are_not_cited(lc_env, monkeypatch, log):
    import app.rag.store as rag_store

    def chunk(cid, title, countries, score):
        return {"id": cid, "doc_id": cid, "score": score, "text": f"{title}: mangrove cover was mapped from imagery.",
                "metadata": {"title": title, "source": "test", **({"countries": countries} if countries else {})}}

    class FakeCorpus:
        def __init__(self, hits):
            self.hits = hits
        def documents(self):
            return [{"doc_id": h["doc_id"]} for h in self.hits]
        def search(self, q, k=5, **kw):
            want = kw.get("countries")
            return [h for h in self.hits if not want or set(h["metadata"].get("countries") or []) & set(want)][:k]
        def raw_path(self, doc_id):
            return None

    hits = [chunk("n1", "Nicoya report", ["Elsewhere"], 0.9), chunk("t1", "Test bay memo", ["Testland"], 0.6),
            chunk("g1", "Global guideline", None, 0.5)]
    monkeypatch.setattr(rag_store, "Corpus", lambda name: FakeCorpus(hits))
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=POLY, stack="landcover_wc")
    titles = [c["title"] for c in p["citations"] if c["kind"] == "document"]
    log("OUTPUT", titles)
    assert "Nicoya report" not in titles and "Test bay memo" in titles and "Global guideline" in titles
    monkeypatch.setattr(rag_store, "Corpus", lambda name: FakeCorpus(hits[:1]))
    q = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=POLY, stack="landcover_wc")
    assert not [c for c in q["citations"] if c["kind"] == "document"]
    assert any("holds no document about Test bay (Testland)" in g and "Elsewhere: Nicoya report" in g for g in q["gaps"])


def test_reference_lists_and_contents_pages_are_recognised_in_any_language():
    # a Spanish reference list: placeholder surnames, the shape of the entries is what counts
    biblio = ("Literatura citada\nApellido, A. 1994. Los manglares del Pacífico centroamericano. Heredia, CR. "
              "EFUNA. 352p.\nAutora, B. 1996. El manglar de un estuario en Costa Rica. Turrialba, CR. CATIE. "
              "91p.\nNombre, C.; Otro, D. 2004. Plan de manejo. San José, CR. SINAC. 120p.\nTercero, E. 1992. "
              "Evaluación de impacto ambiental. Turrialba, CR. 45p.")
    assert lc._looks_like_references(biblio)
    assert lc._looks_like_references("In: Paris: Food and Agriculture Organization")
    assert lc._looks_like_references("Referencias\nMINAE (2019). Informe nacional. San José.\n"
                                     "SINAC (2021). Mapa de ecosistemas. San José.\nFAO (2007). The world's mangroves. Roma.")
    toc = ("Índice\n1. Introducción ........ 3\n2. Área de estudio ........ 5\n3. Métodos ........ 9\n"
           "4. Resultados ........ 14\n5. Discusión ........ 22")
    assert lc._looks_like_references(toc)
    assert not lc._looks_like_references(
        "La cobertura de manglar del Golfo de Nicoya pasó de 15620 ha en 1956 a 13516 ha en 2014, con la "
        "mayor pérdida entre 1956 y 1985 por la expansión de salinas y camaroneras.")
    assert not lc._looks_like_references("Mangle alto: bosque de mangle de más de 10 m de altura, clasificado "
                                         "en 2021 con imágenes de 2019 y 2020.")


# ------------------------------------------------------- 6. area-only answers
def test_a_plain_class_question_outside_every_stack_names_them_all_nearest_first(lc_env, log):
    p = assemble.assemble(pack="land-cover", place="Coast X", geometry=FAR_AWAY, year="2020")
    log("OUTPUT", p.get("note", "")[:300])
    assert p["status"] == "declined" and "outside every registered land-cover stack" in p["note"]
    assert "4 registered stack(s), nearest first" in p["note"]
    for sid in ("landcover_one", "landcover_two", "landcover_wc", "landcover_far"):
        assert sid in p["note"]
    assert p["note"].index("landcover_far") < p["note"].index("landcover_one")
    assert "km away" in p["note"] and "vectors=[...]" in p["note"]


def test_the_target_lets_a_geometry_stand_for_a_place_and_measures_for_a_year():
    t, why = packs.build_target("land-cover", {"place": "", "year": "", "geometry": FAR_AWAY, "vectors": ["x"]})
    assert why is None and t == {}
    t, why = packs.build_target("land-cover", {"place": "", "year": "2020"})
    assert t is None and "needs place (or geometry)" in why
    t, why = packs.build_target("land-cover", {"place": "Bay", "year": ""})
    assert t is None and "needs year (or vectors / layers)" in why
    # another pack's requirements are its own
    t, why = packs.build_target("risk", {"place": "Bay", "geometry": FAR_AWAY})
    assert t is None and "needs hazard" in why


def test_an_area_with_no_stack_still_measures_the_vectors_asked(lc_env, monkeypatch, log):
    w, s_, e, n = FAR_AWAY
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"NAME": "Mangrove stand A"},
         "geometry": mapping(box(w + 0.01, s_ + 0.01, w + 0.02, s_ + 0.02))}]}
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (json.dumps(fc).encode(), "m.geojson"))
    out = staging.submit("vector", {"layer": "test_far_mangroves", "url": "https://x.test/m.geojson",
                                    "title": "Far mangroves (synthetic)", "description": "synthetic", "source": "test",
                                    "license": "CC0-1.0", "vintage": "2026", "name_field": "NAME"}, OWNER)
    assert out["status"] == "approved", out
    spec = packs.PACKS["land-cover"]
    extras = {"geometry": FAR_AWAY, "vectors": ["test_far_mangroves"]}
    target, why = packs.build_target("land-cover", {"place": "", "year": "", **extras})
    assert why is None
    trace = []
    citations, gaps, stats = spec["gather"](target, spec["default_focus"](target), trace, extras)
    assert [c["kind"] for c in citations] == ["area"] and stats["stack"] is None
    assert "the area you supplied" in citations[0]["text"]                  # no place: labelled as the caller's own
    log("OUTPUT", gaps[0][:300])
    assert "no land-cover class is counted in the area you supplied" in gaps[0]
    assert "nearest it: Far stack (synthetic) (landcover_far)" in gaps[0]
    block, _area = vectors_step.attach(spec, target, stats, citations, gaps, ["test_far_mangroves"], extras)
    assert block and block["test_far_mangroves"]["features_inside"] == 1
    assert citations[-1]["kind"] == "vector_summary" and citations[-1]["n"] == 2
    # a class question there still declines; a vector-only call needs no year
    with pytest.raises(ValueError, match="needs year"):
        spec["gather"]({"place": "Coast X"}, "f", [], {"geometry": FAR_AWAY})
    staging.STAGED_VECTORS.clear()


def test_a_covered_area_without_a_year_answers_for_the_area_and_names_the_stacks(lc_env):
    citations, gaps, stats = lc.gather_land_cover_evidence({"place": "Test bay"}, "f", [],
                                                           {"geometry": POLY, "layers": ["landcover_wc_2020"]})
    assert [c["kind"] for c in citations] == ["area"] and stats["_area"]["geometry"]
    assert "no map year was asked" in gaps[0] and "Two-class extent (synthetic) (landcover_two) has 1996, 2020" in gaps[0]


# ------------------------------------------------------- 10. the risk pack outside its maps
def test_the_risk_pack_declines_at_once_outside_its_maps(lc_env, monkeypatch, log):
    def no_fetch(*a, **k):
        raise AssertionError("nothing may be fetched for an area outside the risk pack's maps")
    monkeypatch.setattr(ingest, "_fetch_layer", no_fetch)
    monkeypatch.setattr(ingest, "hazard_clip", no_fetch)
    p = assemble.assemble(pack="risk", place="Test bay", geometry=POLY, hazard="toyfar")
    log("OUTPUT", p.get("note", "")[:300])
    assert p["status"] == "declined" and "outside the risk pack's coverage" in p["note"]
    assert "lon 100.0 to 101.0, lat 10.0 to 11.0" in p["note"]          # read from the catalog, not typed
    assert "land-cover pack" in p["note"] and "vectors=[...]" in p["note"]


def test_risk_coverage_comes_from_each_maps_extent_or_its_file(lc_env, tif_writer):
    path = tif_writer(np.ones((10, 10), "int16"), bounds=(LEFT, TOP - 0.01, LEFT + 0.01, TOP), name="hz.tif")
    tiffs = get_settings().tiffs_dir
    tiffs.mkdir(parents=True, exist_ok=True)
    os.replace(path, tiffs / "hazard_toyhere.tif")
    conf = get_settings().tiffs_config_path
    cat = json.loads(conf.read_text())
    cat["hazard_toyhere"] = {"title": "Toy flood here (synthetic)", "local_path": "tiffs/hazard_toyhere.tif",
                             "legend": {1: "Low"}}
    conf.write_text(json.dumps(cat))
    cov = packs.risk_coverage()
    assert cov["hazard_toyfar"]["bounds"] == [100.0, 10.0, 101.0, 11.0]
    assert cov["hazard_toyhere"]["bounds"][0] == pytest.approx(LEFT)        # from the file's own header
    aoi = ingest.ensure_aoi(geometry=POLY, name="Test bay", layers=())
    assert risk._outside_coverage("hazard_toyhere", aoi, "Test bay") is None
    assert "outside the risk pack's coverage" in risk._outside_coverage("hazard_toyfar", aoi, "Test bay")


# ------------------------------------------------------- 11. the generated texts
def test_exposure_assets_are_the_point_layers_inside_the_risk_region(monkeypatch):
    from app.graph.geo import registry as geo_registry, vectors
    layers = {"th_shelters": {"title": "Shelters (Thailand)", "bbox": [100.2, 13.5, 100.8, 14.0], "countries": ["Thailand"]},
              "cr_occurrences": {"title": "Occurrences (Costa Rica)", "bbox": [-85.3, 9.9, -84.7, 10.3], "countries": ["Costa Rica"]},
              "global_farms": {"title": "Farms (global)", "bbox": [-156.0, -54.0, 166.0, 71.0]}}
    monkeypatch.setattr(vectors, "visible", lambda: layers)
    monkeypatch.setattr(vectors, "family", lambda e: "points")
    monkeypatch.setattr(geo_registry, "countable", lambda: ("hospitals", "schools", "buildings", *layers))
    monkeypatch.setattr(packs, "risk_coverage", lambda: {"hazard_flood": {"title": "Flood", "bounds": [92.0, -11.1, 141.2, 28.7],
                                                                           "countries": []}})
    words = packs._risk_asset_words()
    assert "shelters (thailand)" in words
    assert "occurrences (costa rica)" not in words and "farms (global)" not in words


def test_land_cover_texts_name_stacks_by_region_not_one_run_on_list(lc_env):
    classes, where = packs._lc_stacks_words()
    assert "mangrove" in classes
    assert where.startswith("Testland: One-class extent (synthetic), 1996-2020 (1 class)")
    assert "Farland: Far stack (synthetic), 2020 (2 classes)" in where
    assert "Testland" in packs.text(packs.PACKS["land-cover"], "keywords")
