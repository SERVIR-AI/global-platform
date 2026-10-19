"""Sprint fix round 2, review findings (2026-10-19): retrieval keeps every passage unless
the caller opts in, and the reference test reads only real bibliographies and contents
pages; a clip the host cannot honour is declined in words; a torn zip is declined; an
anonymous caller owns nothing; GDAL reads contributed bytes with one driver chosen from
their magic bytes and no network; a recipe marker anywhere in the header is refused; a
take-down removes only its own landing. All on synthetic data, never presented as real."""
import hashlib
import io
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import yaml
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from app.config import get_settings
from app.contrib import identity, staging
from app.mcp import fetch
from app.rag import filters as rag_filters
from app.rag.store import Corpus

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
CAP = 256 * 1024                       # small, so a synthetic file is "national"
WORLD = (-86.0, 9.0, -84.0, 11.0)
GULF = [-85.2, 10.0, -85.0, 10.2]


# ------------------------------------------------------------ 1. retrieval, opt-in skip
class WordEmbedder:
    """A bag of words on 1,024 hashed dimensions — no network."""
    provider, model = "test", "words-v1"

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(1024, np.float32)
            for w in re.findall(r"[a-záéíóúñ]+", t.lower()):
                v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % 1024] += 1
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return np.stack(out)


# Shapes measured on the real libraries, with made-up figures and sources: each is a
# passage with findings that the old heuristic passed over.
DATED_REPORT = (
    "Thematic Report | 16 January 2025\nCRISIS IMPACTS\nFood security\nThe drought has aggravated "
    "food insecurity. Between October 2024 and March 2025 an estimated 5.1 million people (31% of "
    "the analysed population) will face high levels of food insecurity, including 210,000 people at "
    "Emergency (IPC Phase 4) levels (IPC 02/10/2024; StatsOffice accessed 10/12/2024). Food prices "
    "rose 17.9% between November 2023 and November 2024 (StatsOffice 10/12/2024; Inst 21/11/2024). "
    "The 2023-2024 season was the poorest since 2019, after 2015-2016 and 1991-1992, and grain "
    "output fell by half against 2022-2023 (Agency 16/12/2024; Agency 07/05/2024; Govt 16/04/2024).")
STATS_TABLE = (
    "Table 3.7: Production and Value of Millet, 2020-2024\nYear Units 2020 2021 2022 2023 2024*\n"
    "Area Hectares 116,000 126,000 108,000 129,000 131,000\nProduction Tonnes 81,000 92,000 70,000 "
    "99,000 101,000\nValue KSh million 2019 2020 2021 2022 2023\nSource: State Department, 2024")
CONTENTS_TAIL = (
    "Purpose 3\nMethodology 3\nKey Messages 4\nSouthern Africa 5\nEastern Africa 6\nContents\n\n"
    "Methodology\nThe study examines the main impacts of the 2015/16 and 2023/24 El Niño events on "
    "drought, flooding, displacement and the agriculture sector, to show what a future El Niño "
    "could bring to each region. Key messages: about 60 million people were affected in 2015/16 "
    "and around 50 million in 16 countries by April 2024; the worst-affected countries were chosen "
    "by severity of impacts, people affected and the level of food insecurity.")
LEADER_TABLE = ("Maize ........ 1,234\nSorghum ........ 5.8\nBeans ........ 12 34\nCassava ........ 2,001\n"
                "Rice ........ 0.75")
CITED_PROSE = (
    "There is an urgent need to evaluate the combined impacts of climate change and infrastructure "
    "basin-wide (Alpha et al., 2019; Beta et al., 2010; Gamma et al., 2012). Existing studies focus "
    "on basin flows (Delta et al., 2018; Alpha et al., 2016; Epsilon et al., 2018) or on the lake "
    "(Zeta et al., 2012; Eta et al., 2021; Theta et al., 2018; Iota et al., 2019); very little is "
    "known of the floodplain itself.")
LABEL_SHORT = ("ENSO Alert System Status: El Niño Advisory\nSynopsis: El Niño conditions are present and "
               "expected to strengthen into the Northern Hemisphere winter 2026-27.")
URL_LIST = "\n".join(
    f"AGENCY ({d} 20{y}). Report {y} on the season. URL: https://example.org/reports/{y}/season-{y}"
    for d, y in (("March", 16), ("18 Mar", 16), ("13 Aug", 24), ("31 Jan", 16), ("11 Dec", 23),
                 ("May", 24), ("30 Aug", 24)))
AUTHOR_LIST = ("Apellido, A. B., Autora, C., et al. (2012). Carbon stocks. Estuaries 35, 1-10. doi:10.1/x\n"
               "Nombre, D. (2010) Wetlands. Tercero, E. et al. 2008 Global Biogeochem. doi:10.1/y\n"
               "Cuarto, F. G. et al. (2011) Nat. Geosci. 4, 293. Quinto, H. et al. 2011 Wetlands 31, 343.")
REFS_HEADED = ("Report of the season 14\nReferences\nAGENCY (2019). Informe nacional. San José.\n"
               "OFFICE (2021). Mapa de ecosistemas. San José.\nFAO (2007). The world's mangroves. Roma.")
TOC_LEADERS = ("CHAPTER 2: APPROACH ............................ 13\n2.0. OVERVIEW .................... 14\n"
               "2.1. APPROACH ...................... 14\n2.1.1. Roadmap ..................... 14\n"
               "2.1.2. Funding ...................... 15")
TOC_HEADED = "Table of Contents\nPurpose 3\nMethodology 3\nKey Messages 4\nSouthern Africa 5\nEastern Africa 6"


def test_the_reference_test_reads_real_bibliographies_and_contents_pages_only(log):
    """Years alone, a contents tail before findings, table rows with leaders, prose citing
    in parentheses and a 'Synopsis:' label are passages; author or link lists, a references
    heading at the top and contents pages are not."""
    for name, text in (("dated report", DATED_REPORT), ("statistics table", STATS_TABLE),
                       ("contents tail then findings", CONTENTS_TAIL), ("leader table", LEADER_TABLE),
                       ("prose citing in parentheses", CITED_PROSE), ("short labelled lines", LABEL_SHORT)):
        log("CHECK", f"kept: {name}")
        assert not rag_filters.looks_like_references(text), name
    for name, text in (("link list", URL_LIST), ("author list", AUTHOR_LIST), ("references heading", REFS_HEADED),
                       ("dotted contents", TOC_LEADERS), ("headed contents", TOC_HEADED),
                       ("imprint fragment", "In: Paris: Food and Agriculture Organization")):
        log("CHECK", f"passed over: {name}")
        assert rag_filters.looks_like_references(text), name


@pytest.fixture
def library(tmp_path, monkeypatch):
    import app.rag.embed as rag_embed
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "rag_min_relevance", 0.01)
    monkeypatch.setattr(rag_embed, "get_embedder", lambda: WordEmbedder())

    def fill(name, docs):
        c = Corpus(name)
        for text, meta in docs:
            c.ingest(text, {"source": "test", "temporal": "retrospective", **meta})
        return Corpus(name)
    return fill


DOCS = ((DATED_REPORT, {"title": "Drought impact update"}), (STATS_TABLE, {"title": "Production report"}),
        (CONTENTS_TAIL, {"title": "Lessons for preparedness"}), (AUTHOR_LIST, {"title": "Carbon paper, references"}))
Q = "drought food insecurity millet production impacts season carbon"


@pytest.mark.parametrize("pack", ["food-security", "risk", "land-cover"])
def test_the_library_skips_nothing_unless_the_caller_opts_in(library, pack, log):
    """Every pack's library: the default keeps every passage; opting in passes over the
    bibliography only — the dated report, the table and the contents tail stay."""
    c = library(pack, DOCS)
    every = {h["metadata"]["title"] for h in c.search(Q, k=10)}
    skipped = {}
    sifted = {h["metadata"]["title"] for h in c.search(Q, k=10, skip_references=True, skipped=skipped)}
    log("OUTPUT", f"{pack}: default {sorted(every)}; opted in {sorted(sifted)}")
    assert every == {t for _, m in DOCS for t in [m["title"]]}
    assert sifted == every - {"Carbon paper, references"}
    assert skipped["references"] == ["Carbon paper, references"]


def test_corpus_search_opts_in_and_keeps_a_freshly_contributed_dated_report(library, log):
    """corpus_search passes over the bibliography and says so; a contributed report full of
    dates (not a bibliography) is returned, not passed over."""
    library("food-security", DOCS + ((DATED_REPORT.replace("Thematic", "Contributed"),
                                      {"title": "Contributed update", "contribution_id": "c0ffee01"}),))
    out = fetch.search(Q, k=10)
    titles = [h["passport"]["title"] for h in out["hits"]]
    log("OUTPUT", titles)
    assert "Contributed update" in titles and "Drought impact update" in titles
    assert "Carbon paper, references" not in titles
    assert "1 reference-list or contents passage(s)" in out["passed_over"]


# ------------------------------------------------------------ a local host, no network
class _Host:
    """Files by path, each served one way: 'ranges' (answers Range, sends a size),
    'whole' (ignores Range, sends a size) or 'streamed' (ignores Range, chunked, no size)."""

    def __init__(self):
        self.files, self.mode, self.port = {}, {}, None

    def put(self, name, data, mode="ranges"):
        self.files["/" + name], self.mode["/" + name] = data, mode
        return f"http://127.0.0.1:{self.port}/{name}"


def _handler(host):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, body):
            path = self.path.split("?")[0]
            data, mode = host.files.get(path), host.mode.get(path)
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            rng = self.headers.get("Range") if mode == "ranges" else None
            if rng:
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                a = int(m.group(1))
                b = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                part = data[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            else:
                part = data
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if mode == "streamed":
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            if not body:
                return
            try:
                for i in range(0, len(part), 65536):
                    chunk = part[i:i + 65536]
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
                                     if mode == "streamed" else chunk)
                if mode == "streamed":
                    self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self._send(True)

        def do_HEAD(self):
            self._send(False)
    return H


@pytest.fixture
def host():
    h = _Host()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(h))
    h.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield h
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A hermetic catalog and store; the real fetch policy with loopback allowed."""
    s = get_settings()
    cat = tmp_path / "tiffs.yml"
    cat.write_text(yaml.safe_dump({}))
    sch = tmp_path / "raster_schema.yml"
    sch.write_text(yaml.safe_dump({"defaults": {"crs": "EPSG:4326", "float_tol": 0.0001}, "layers": {}}))
    for k, v in {"cache_dir": tmp_path, "tiffs_dir": tmp_path / "tiffs",
                 "tiffs_config_path": cat, "raster_schema_path": sch,
                 "tiffs_contrib_path": tmp_path / "tiffs.contrib.yml",
                 "raster_schema_contrib_path": tmp_path / "raster_schema.contrib.yml",
                 "grp_mattermost_webhook": "", "grp_contrib_allow_private_hosts": True,
                 "grp_contrib_max_bytes": CAP, "grp_contrib_hourly_cap": 0}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(staging, "STAGED_RASTERS", {})
    monkeypatch.setattr(staging, "_STAGED_LOADED", False)
    return tmp_path


def _tif(arr, bounds=WORLD):
    h, w = arr.shape
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=w, height=h, count=1, dtype=arr.dtype.name, crs="EPSG:4326",
                     transform=from_bounds(*bounds, w, h), tiled=True, blockxsize=256, blockysize=256,
                     compress="deflate") as d:
            d.write(arr, 1)
        return mf.read()


def _noise(n=1000, seed=0):
    return np.random.default_rng(seed).uniform(0, 1800, (n, n)).astype("float32")


def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _measure(layer, url, **over):
    return {"layer": layer, "url": url, "title": f"{layer} (synthetic)", "description": "synthetic test layer",
            "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10", "countries": ["Testland"],
            "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 1800, "units": "m"}, **over}


def _submit(m):
    tok = identity.bind(OWNER)
    try:
        return staging.submit("raster", m, OWNER)
    finally:
        identity.unbind(tok)


def _no_strays(env):
    return not list((env / "tiffs").glob("staged-tmp-*")) if (env / "tiffs").exists() else True


# ------------------------------------------------------------ 2. a clip the host cannot honour
def test_a_given_clip_from_a_host_that_streams_without_a_size_is_declined_in_words(env, host, log):
    """The host neither answers range requests nor says the size: with the clip already
    given, the decline says why it cannot be read remotely and what to do — it never asks
    for the clip again (the agent would loop)."""
    url = host.put("big.tif", _tif(_noise()), mode="streamed")
    out = _submit(_measure("measure_streamed", url, clip=GULF))
    said = out["problems"][0]
    log("OUTPUT", said)
    assert out["status"] == "declined" and not said.startswith("missing required field 'clip'")
    assert "sends it only whole" in said and "or say the file's size" in said
    assert "download it, clip it" in said and "Google Drive" in said
    # no clip yet: the same host still gets the clip asked for, as a missing field
    ask = _submit(_measure("measure_streamed", url))
    assert ask["problems"][0].startswith("missing required field 'clip'")
    # a host that says the size but sends only whole files keeps its sized wording
    sized = _submit(_measure("measure_sized", host.put("sized.tif", _tif(_noise()), mode="whole"), clip=GULF))
    assert "sends it only whole (it does not answer range requests)" in sized["problems"][0]
    assert _no_strays(env)


# ------------------------------------------------------------ 3. a torn zip
def test_a_truncated_zip_is_declined_not_raised(env, host, log):
    """A zip cut short under the cap: 're-upload it', not a raw BadZipFile out of the
    tool — for a raster and for a zipped shapefile alike."""
    data = _zip({"maps/gulf.tif": _tif(_noise(100))})
    out = _submit(_measure("measure_cut", host.put("cut.zip", data[: len(data) // 2], mode="whole")))
    log("OUTPUT", out["problems"])
    assert out["status"] == "declined"
    assert "cannot be read (it is truncated or damaged); re-upload it" in out["problems"][0]
    assert "staged-tmp" not in out["problems"][0] and _no_strays(env)
    from app.graph.geo import vecio
    shp = _zip({"rios.shp": b"x" * 400, "rios.dbf": b"x" * 400})
    with pytest.raises(ValueError, match="re-upload it"):
        vecio.to_geojson(shp[: len(shp) // 2], "rios.zip")


# ------------------------------------------------------------ 4. who may take a layer down
LINES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"NOMBRE": "Río Uno"},
     "geometry": {"type": "LineString", "coordinates": [[-85.2, 10.1], [-85.0, 10.2]]}}]}


@pytest.fixture
def vec_env(tmp_path, monkeypatch):
    """Vector contributions land at once (auto-approve), fetched from a fixed body."""
    import json
    from app.contrib import fetch_policy
    from app.mcp import store
    s = get_settings()
    for k, v in {"cache_dir": tmp_path / "cache", "tiffs_dir": tmp_path / "cache" / "tiffs",
                 "grp_mattermost_webhook": "", "grp_auto_approve": True, "grp_contrib_hourly_cap": 0,
                 "grp_oauth_enabled": False, "grp_allow_anonymous": False, "grp_reviewers": []}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (json.dumps(LINES).encode(), "l.geojson"))
    monkeypatch.setenv("GRP_AUTO_APPROVE", "1")
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    yield s
    staging.STAGED_VECTORS.clear()


def _land_lines(layer, caller):
    out = staging.submit("vector", {"layer": layer, "url": f"https://x.test/{layer}.geojson",
                                    "title": f"{layer} (synthetic)", "description": "synthetic lines",
                                    "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10"}, caller)
    assert out["status"] == "approved", out
    return out["contribution_id"]


def test_an_anonymous_caller_owns_nothing_and_takes_nothing_down(vec_env, monkeypatch, log):
    """An open deployment: every caller is 'anonymous', so none owns a contribution — one
    stranger cannot withdraw another's live layer, nor review it whatever GRP_REVIEWERS says."""
    from app.graph.geo import vectors
    monkeypatch.setattr(vec_env, "grp_allow_anonymous", True)
    a = identity.resolve()
    assert a.source == "anonymous" and not a.owns("anonymous") and not a.owns(None)
    assert not identity.Caller("anonymous", "anonymous", False, "bound").owns("anonymous")
    cid = _land_lines("anon_lines", a)
    b = identity.resolve()                           # another participant, the same id
    out = staging.withdraw(cid, caller=b)
    log("OUTPUT", out["note"])
    assert out["status"] == "declined" and "anonymous caller" in out["note"]
    assert staging.status(cid, caller=b)["status"] == "declined"
    assert staging.status(caller=b)["contributions"] == []
    assert "anon_lines" in vectors.landed()
    from app.mcp import store
    assert store.load_contribution(cid)["status"] == "approved"
    monkeypatch.setattr(vec_env, "grp_reviewers", ["anonymous"])
    assert not identity.resolve().is_reviewer


def test_the_local_operator_and_a_signed_in_contributor_still_withdraw_their_own(vec_env, log):
    """A dev box's one operator (local-dev) withdraws what it landed; under OAuth only the
    contributor may — another signed-in caller is told nothing of it."""
    from app.graph.geo import vectors
    me = identity.resolve()
    assert me.source == "local-dev" and me.owns("local-dev")
    cid = _land_lines("dev_lines", me)
    out = staging.withdraw(cid, caller=me)
    log("OUTPUT", out.get("unlanded"))
    assert out["status"] == "withdrawn" and "dev_lines" not in vectors.landed()
    alice = identity.Caller("user_alice", "alice@hub.test", False, "token")
    bob = identity.Caller("user_bob", "bob@hub.test", False, "token")
    cid = _land_lines("alice_lines", alice)
    assert staging.withdraw(cid, caller=bob) == {"status": "declined", "note": f"unknown contribution {cid!r}"}
    assert staging.withdraw(cid, caller=alice)["status"] == "withdrawn"


# ------------------------------------------------------------ 7. a take-down removes only its own
def test_a_late_take_down_never_removes_the_layer_that_replaced_it(vec_env, log):
    """Two take-downs of one approved contribution race a resubmission of the freed name:
    the late one finds the NEW contributor's row, says so and removes nothing."""
    from app.graph.geo import vectors
    from app.mcp import store
    alice = identity.Caller("user_alice", "alice@hub.test", False, "token")
    bob = identity.Caller("user_bob", "bob@hub.test", False, "token")
    first = _land_lines("shared_name", alice)
    stale = store.load_contribution(first)            # what the racing second take-down read
    assert staging.withdraw(first, caller=alice)["status"] == "withdrawn"
    second = _land_lines("shared_name", bob)
    undone, why = staging._take_down(stale)
    log("OUTPUT", undone)
    assert why is None and undone["removed"] is False and undone["already_replaced"] is True
    assert second in undone["note"] and "nothing was removed" in undone["note"]
    assert vectors.landed()["shared_name"]["contribution_id"] == second


def test_feed_and_raster_take_downs_check_the_row_is_still_theirs(env, host, monkeypatch, log):
    """A raster row and a feed row carry the contribution that landed them; a take-down of
    another contribution leaves them. A table row from before rows carried the id is
    matched on the sha its landing recorded."""
    import yaml
    from app.graph.geo import tiffs
    s = get_settings()
    monkeypatch.setattr(s, "grp_auto_approve", True)
    monkeypatch.setattr(s, "feeds_conf_dir", env / "feeds")
    url = host.put("small.tif", _tif(_noise(120)))
    out = _submit(_measure("measure_small", url))
    assert out["status"] == "approved", out
    row = tiffs.catalog(include_staged=False)["measure_small"]
    assert row["contribution_id"] == out["contribution_id"]
    stale = {"contribution_id": "0" * 16, "kind": "raster", "manifest": {"layer": "measure_small"}}
    undone, why = staging._take_down(stale)
    assert undone["already_replaced"] and "measure_small" in tiffs.catalog(include_staged=False)
    # feed rows: one of another contribution, and a table row with no id (older landing)
    (env / "feeds").mkdir()
    feed = {"dataset": "test_feed", "contributed": True, "contribution_id": "1" * 16,
            "fetch": {"url": "https://x.test/new.json"}}
    (env / "feeds" / "test_feed.yml").write_text(yaml.safe_dump(feed))
    undone, _ = staging._take_down({"contribution_id": "2" * 16, "kind": "feed",
                                    "manifest": {"dataset": "test_feed", "fetch": {"url": "https://x.test/new.json"}}})
    assert undone["already_replaced"] and (env / "feeds" / "test_feed.yml").exists()
    table = {"dataset": "test_table", "contributed": True, "fetch": {"path": "tables/t.csv", "sha256": "b" * 64}}
    (env / "feeds" / "test_table.yml").write_text(yaml.safe_dump(table))
    old = {"contribution_id": "3" * 16, "kind": "table", "manifest": {"dataset": "test_table"},
           "landing": {"sha256": "a" * 64}}
    undone, _ = staging._take_down(old)
    log("OUTPUT", undone["note"])
    assert undone["already_replaced"] and (env / "feeds" / "test_table.yml").exists()
    undone, why = staging._take_down({**old, "landing": {"sha256": "b" * 64}})
    assert why is None and undone["removed"] is True and not (env / "feeds" / "test_table.yml").exists()
