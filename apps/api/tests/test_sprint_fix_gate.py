"""Mangrove sprint dry run, the contribution gate: what a participant hit fetching,
landing and querying real sources — each fixed at the gate every kind and every pack
passes through, and exercised here on synthetic data with no network.

Fetching: an honest User-Agent first and one browser retry; a bot challenge, a login
wall and an upstream's own error message are said in the contributor's words; a
signed redirect URL is never echoed. Documents: a web page is not a PDF, a Google
Doc is fetched as an export, the BOM goes. Tables: any 8-bit encoding, rows counted
by the csv reader, a declared scope, filters at query time. Routing: a licence that
forbids redistribution, or personal e-mail addresses, wait for a human even with
auto-approve on. Hints point at a pack that reaches the layer; the audit counts
vectors; the hourly cap is a setting, atomic, and says when a slot frees."""
import copy
import hashlib
import ipaddress
import json
import threading
import time

import numpy as np
import pytest
import yaml
from requests.structures import CaseInsensitiveDict

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging, tables
from app.mcp import feeds, registry, store
from app.rag import docloader
from app.rag.embed import ProviderEmbedder

OWNER = identity.Caller("user_owner", "scientist@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "analyst@hub.test", False, "bound")


# ------------------------------------------------------------- scripted transport
class Resp:
    def __init__(self, status=200, headers=None, body=b"", reason=""):
        self.status_code, self.reason = status, reason
        self.headers = CaseInsensitiveDict(headers or {})
        self._body = body

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class Net:
    """script: {host+path: fn(user_agent) -> Resp}; records (host, path, UA) per call."""
    script, calls = {}, []

    def __init__(self, hostname, scheme):
        pass

    def get(self, url, timeout=None, stream=None, allow_redirects=None, headers=None):
        path = "/" + url.split("/", 3)[-1].split("?")[0]
        Net.calls.append((headers["Host"], path, headers["User-Agent"]))
        return Net.script[headers["Host"] + path](headers["User-Agent"])

    def close(self):
        pass


@pytest.fixture
def net(monkeypatch):
    Net.script, Net.calls = {}, []
    monkeypatch.setattr(fetch_policy, "_addresses", lambda host: [ipaddress.ip_address("93.184.216.34")])
    monkeypatch.setattr(fetch_policy, "_session", lambda hostname, scheme: Net(hostname, scheme))
    return Net


def _browser(ua):
    return ua == fetch_policy.BROWSER_AGENT


# ------------------------------------------------------------- 2. user agent
def test_an_honest_agent_goes_first_and_a_403_is_retried_once_as_a_browser(net, log):
    """zenodo.org answered the browser string 403 and the honest one with the file;
    another host may do the opposite — each gets exactly what works, one retry."""
    net.script["zenodo.example/f.zip"] = lambda ua: (Resp(403, {"content-type": "text/html"}, b"<html>403</html>")
                                                     if _browser(ua) else Resp(200, {}, b"PK\x03\x04zip"))
    assert fetch_policy.fetch("https://zenodo.example/f.zip", max_bytes=1000)[0] == b"PK\x03\x04zip"
    assert len(net.calls) == 1 and "SERVIR" in net.calls[0][2]
    net.calls.clear()
    net.script["picky.example/f.csv"] = lambda ua: (Resp(200, {}, b"a,b\n1,2\n") if _browser(ua)
                                                    else Resp(403, {}, b""))
    assert fetch_policy.fetch("https://picky.example/f.csv", max_bytes=1000)[0] == b"a,b\n1,2\n"
    log("OUTPUT", [c[2][:20] for c in net.calls])
    assert [_browser(c[2]) for c in net.calls] == [False, True]


# ------------------------------------------------------------- 3. bot challenges
@pytest.mark.parametrize("status,headers,body,said", [
    (202, {"x-amzn-waf-action": "challenge", "content-type": "text/html"}, b"", "x-amzn-waf-action"),
    (403, {"cf-mitigated": "challenge", "content-type": "text/html"},
     b"<!DOCTYPE html><html><head><title>Just a moment...</title>", "cf-mitigated"),
    (200, {"content-type": "text/html"}, b"<!DOCTYPE html><html><head><title>Client Challenge</title>",
     "a 'Client Challenge' page"),
    (200, {"content-type": "text/csv"}, b"", "empty response"),
])
def test_a_bot_challenge_answered_2xx_is_refused_as_one(net, status, headers, body, said, log):
    net.script["ndownloader.example/files/1"] = lambda ua: Resp(status, headers, body)
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://ndownloader.example/files/1", max_bytes=1000)
    log("OUTPUT", str(exc.value))
    assert "bot challenge" in str(exc.value) and said in str(exc.value)
    assert "Google Drive (Anyone with the link)" in str(exc.value)
    assert len(net.calls) == 2                       # retried once, as a browser


# ------------------------------------------------------------- 4. login walls
def test_a_redirect_to_a_login_ends_as_a_login_wall_not_a_portal_page(net, log):
    net.script["daac.example/protected/agb.tif"] = lambda ua: Resp(
        302, {"location": "https://urs.earthdata.nasa.gov/oauth/authorize?client_id=x&state=y"})
    net.script["urs.earthdata.nasa.gov/oauth/authorize"] = lambda ua: (
        Resp(200, {"content-type": "text/html"}, b"<!DOCTYPE html><html>Earthdata Login</html>")
        if _browser(ua) else Resp(401, {"content-type": "text/html"}, b"HTTP Basic: Access denied.\n"))
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://daac.example/protected/agb.tif", max_bytes=1000)
    msg = str(exc.value)
    log("OUTPUT", msg)
    assert msg.startswith("this file needs a login at urs.earthdata.nasa.gov")
    assert "Google Drive (Anyone with the link)" in msg and "portal" not in msg
    # a generic sign-in path on any host is a wall too
    net.script["repo.example/files/x.pdf"] = lambda ua: Resp(302, {"location": "/cas/login?service=x"})
    net.script["repo.example/cas/login"] = lambda ua: Resp(200, {"content-type": "text/html"}, b"<html>Sign in</html>")
    with pytest.raises(fetch_policy.FetchRefused, match="needs a login at repo.example"):
        fetch_policy.fetch("https://repo.example/files/x.pdf", max_bytes=1000)


def test_a_redirect_through_an_identity_provider_that_hands_back_the_file_is_fine(net):
    """Springer passes every download through idp.../authorize and back to the PDF."""
    net.script["pub.example/content/pdf/a.pdf"] = lambda ua: (
        Resp(303, {"location": "https://idp.pub.example/authorize?x=1"}) if len(net.calls) == 1 else
        Resp(200, {"content-type": "application/pdf"}, b"%PDF-1.4 body"))
    net.script["idp.pub.example/authorize"] = lambda ua: Resp(302, {"location": "https://pub.example/content/pdf/a.pdf"})
    assert fetch_policy.fetch("https://pub.example/content/pdf/a.pdf", max_bytes=1000)[0] == b"%PDF-1.4 body"


# ------------------------------------------------------------- 5 + 6. errors, signed URLs
def test_the_upstream_message_is_quoted_short_and_clean(net, log):
    body = json.dumps({"status": "ERROR", "message": "You may not download this file without the "
                       "required Guestbook response for guestbookID 469.\x07"}).encode()
    net.script["dataverse.example/api/access/datafile/7"] = lambda ua: Resp(
        400, {"content-type": "application/json;charset=UTF-8"}, body, "Bad Request")
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://dataverse.example/api/access/datafile/7", max_bytes=1000)
    msg = str(exc.value)
    log("OUTPUT", msg)
    assert "HTTP 400 Bad Request" in msg and "Guestbook response for guestbookID 469." in msg
    assert "\x07" not in msg
    net.script["api.example/x"] = lambda ua: Resp(500, {"content-type": "text/plain"}, b"z" * 5000)
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://api.example/x", max_bytes=1000)
    assert len(str(exc.value).split("the site says: ")[1]) <= 202     # 200 + quotes


def test_a_signed_redirect_url_is_never_echoed(net, log):
    signed = ("https://bucket.s3.example/hdx/cri_admin.zip?AWSAccessKeyId=AKIASECRET"
              "&Signature=s1gn&x-amz-security-token=t0ken")
    net.script["data.hdx.example/dataset/cod/download/cri_admin.zip"] = lambda ua: Resp(302, {"location": signed})
    net.script["bucket.s3.example/hdx/cri_admin.zip"] = lambda ua: Resp(200, {"content-length": "145246475"}, b"")
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://data.hdx.example/dataset/cod/download/cri_admin.zip", max_bytes=1000)
    msg = str(exc.value)
    log("OUTPUT", msg)
    assert msg.startswith("https://data.hdx.example/dataset/cod/download/cri_admin.zip declares 145,246,475")
    assert "one layer per file" in msg and "clip" in msg
    assert not any(s in msg for s in ("AKIASECRET", "Signature", "s1gn", "t0ken", "amz"))
    # an error at the signed hop names the hop without its query
    net.script["bucket.s3.example/hdx/cri_admin.zip"] = lambda ua: Resp(
        403, {"content-type": "application/xml"},
        b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>")
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://data.hdx.example/dataset/cod/download/cri_admin.zip", max_bytes=1000)
    msg = str(exc.value)
    assert "https://bucket.s3.example/hdx/cri_admin.zip answered HTTP 403" in msg
    assert "Request has expired" in msg and "Signature" not in msg and "AKIA" not in msg


# ------------------------------------------------------------- 8. Google Docs
def test_a_google_doc_is_fetched_as_an_export_and_a_private_one_is_a_login_wall(net, log):
    did = "1heQ5ys5Lauee5dU5lVyQuM6HunZs-WrNMh6GK1Jt4ZQ"
    assert fetch_policy.direct_url(f"https://docs.google.com/document/d/{did}/edit?usp=sharing") == \
        f"https://docs.google.com/document/d/{did}/export?format=docx"
    keep = f"https://docs.google.com/document/d/{did}/export?format=txt"
    assert fetch_policy.direct_url(keep) == keep                       # a chosen format stays
    assert fetch_policy.drive_file_id(f"https://docs.google.com/document/d/{did}/edit") is None
    assert "Google Sheets" in fetch_policy.check_url(f"https://docs.google.com/spreadsheets/d/{did}/edit")[0]
    net.script[f"docs.google.com/document/d/{did}/export"] = lambda ua: Resp(
        302, {"location": "https://accounts.google.com/ServiceLogin?continue=https://docs.google.com/x"})
    net.script["accounts.google.com/ServiceLogin"] = lambda ua: Resp(
        200, {"content-type": "text/html"}, b"<!doctype html><html>Sign in</html>")
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch(f"https://docs.google.com/document/d/{did}/edit", max_bytes=1000)
    log("OUTPUT", str(exc.value))
    assert "needs a login at accounts.google.com" in str(exc.value) and "Anyone with the link" in str(exc.value)


# ------------------------------------------------------------- 7 + 8. docloader
def test_docloader_reads_what_the_bytes_are():
    assert docloader.extract_text("﻿Esta carpeta contiene las capas".encode("utf-8"), "LEAME.txt") \
        .startswith("Esta carpeta")
    with pytest.raises(docloader.WebPage):
        docloader.extract_text(b"<!DOCTYPE html>\n<html><title>Client Challenge</title></html>", "paper.pdf")
    with pytest.raises(docloader.WebPage):
        docloader.extract_text(b"\xef\xbb\xbf  <!-- x --><html><body>landing</body></html>", "5954")
    text, enc = docloader.decode_text("Caño Negro, Golfo de Nicoya, área".encode("mac_roman"))
    assert enc == "mac_roman" and "Caño Negro" in text and "área" in text
    text, enc = docloader.decode_text("Caño Negro — área".encode("cp1252"))
    assert enc == "cp1252" and "Caño Negro — área" == text


# ------------------------------------------------------------- shared gate env
class HashEmbedder:
    def embed(self, texts):
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha1(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).standard_normal(64).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """A hermetic gate: tmp cache, store and feeds dir; fetch answers from `bodies`."""
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path)
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "tiffs")
    monkeypatch.setattr(s, "feeds_conf_dir", tmp_path / "feeds")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(s, "grp_contrib_hourly_cap", 30)
    monkeypatch.setattr(s, "rag_min_relevance", -1.0)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(ProviderEmbedder, "embed", lambda self, texts: HashEmbedder().embed(texts))
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    bodies = {}
    monkeypatch.setattr(fetch_policy, "fetch",
                        lambda url, max_bytes=None: (bodies[url], url.rsplit("/", 1)[-1]))
    snapshot = copy.deepcopy(registry.FEEDS)
    staging.STAGED_FEEDS.clear()
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    yield bodies
    registry.FEEDS.clear()
    registry.FEEDS.update(snapshot)
    staging.STAGED_FEEDS.clear()
    staging.STAGED_VECTORS.clear()


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)


def _doc(url, pack, **over):
    return {"pack": pack, "url": url, "source": "CATIE", "title": "Memoria técnica (test copy)",
            "pub_date": "unstated", "temporal": "retrospective", "validation": "single-agency", **over}


PROSE = ("Memoria técnica del mapa de ecosistemas de manglar 2021. Exactitud global 70,7 %. "
         "El área de manglar es 52.802 ha en el Pacífico. ")


# ------------------------------------------------------------- 7 + 12b + 13. documents
def test_a_web_page_is_not_a_document_and_the_hint_names_the_library(gate, log):
    gate["https://repo.example/5954"] = b"<!DOCTYPE html><html><head><title>Thesis</title></head></html>"
    out = staging.submit("document", _doc("https://repo.example/5954", "land-cover"), OWNER)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    assert "returned a web page, not a document" in out["problems"][0]
    assert "give the PDF's own link" in out["problems"][0]
    # a document that lands: the hint names the library it went to (beyond food-security)
    gate["https://repo.example/memoria.txt"] = (PROSE * 3).encode()
    ok = staging.submit("document", _doc("https://repo.example/memoria.txt", "risk"), OWNER)
    log("OUTPUT", ok["preview"]["how_to_test"])
    assert ok["status"] == "staged" and "pack='risk'" in ok["preview"]["how_to_test"]


def test_personal_email_addresses_hold_a_document_even_under_auto_approve(gate, auto, log):
    staff = "Equipo: Ana Mora (amora@catie.example), Luis Solís (lsolis@sinac.example). "
    gate["https://repo.example/staff.txt"] = (PROSE + staff).encode()
    out = staging.submit("document", _doc("https://repo.example/staff.txt", "land-cover"), OWNER)
    log("OUTPUT", out.get("review"))
    assert out["status"] == "staged" and "2 e-mail addresses" in out["held_for_review"]
    assert store.load_contribution(out["contribution_id"])["status"] == "pending"
    assert "2 e-mail addresses" in staging.status(out["contribution_id"], OWNER)["contribution"]["held_for_review"]
    # one address (a publisher's contact) is not personal data of the kind held
    gate["https://repo.example/one.txt"] = (PROSE + "Contacto: info@catie.example").encode()
    one = staging.submit("document", _doc("https://repo.example/one.txt", "land-cover"), OWNER)
    assert one["status"] == "approved" and "held_for_review" not in one


# ------------------------------------------------------------- 12a. licences
GEOJSON = json.dumps({"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": "A"}, "geometry": {"type": "Point", "coordinates": [-85.2, 10.1]}},
    {"type": "Feature", "properties": {"name": "B"}, "geometry": {"type": "Point", "coordinates": [-85.0, 10.2]}}]}).encode()


def _vector(layer, licence, url="https://x.example/pts.geojson"):
    return {"layer": layer, "url": url, "title": f"{layer} (synthetic)", "description": "synthetic points",
            "source": "test fixture", "license": licence, "vintage": "2026-10", "countries": ["Costa Rica"]}


def test_a_licence_that_forbids_redistribution_waits_for_a_human(gate, auto, log):
    gate["https://x.example/pts.geojson"] = GEOJSON
    wcmc = ("UNEP-WCMC Terms and Conditions of Use for WCMC-011: no commercial use; no sub-licensing or "
            "redistribution (including via web services) without written permission of UNEP-WCMC and ISME")
    held = staging.submit("vector", _vector("test_wam_pts", wcmc), OWNER)
    log("OUTPUT", held.get("review"))
    assert held["status"] == "staged" and "restricts redistribution" in held["held_for_review"]
    assert "no sub-licensing or redistribution" in held["held_for_review"]
    # non-commercial alone lands as before
    nc = staging.submit("vector", _vector("test_nc_pts", "CC-BY-NC-4.0"), OWNER)
    assert nc["status"] == "approved"
    # a table under a no-redistribution licence is held too (any kind with a licence)
    t = staging.submit("table", {**_table("test_worldclim_bands"), "license":
                                 "WorldClim terms: redistribution or commercial use not allowed without prior permission"}, OWNER)
    assert t["status"] == "staged" and "restricts redistribution" in t["held_for_review"]


# ------------------------------------------------------------- 9 + 10 + 11. tables
MAC_CSV = ("Source,Country_Territory,Site,Latitude_decimal_degrees,Longitude_decimal_degrees,AGB_Mg_ha,Notes\n"
           "Cifuentes,Costa Rica,Térraba-Sierpe,8.95,-83.55,210.5,\"core taken\nat low tide\"\n"
           "Cifuentes,Costa Rica,Golfo de Nicoya,10.05,-85.10,180.2,ok\n"
           "Rovai,Panamá,Bahía Panamá,8.90,-79.40,150.0,ok\n"
           "Rovai,Ecuador,Golfo de Guayaquil,-2.60,-80.00,120.0,ok\n")


def _table(ds, **over):
    return {"dataset": ds, "pack": "land-cover", "title": "Mangrove plot structure (synthetic)",
            "description": "SYNTHETIC plot rows for the gate tests", "source": "test fixture",
            "validation": "unvalidated", "license": "CC-BY-4.0", "vintage": "2021-03",
            "cadence": "irregular", "columns": {"country": "Country_Territory", "site": "Site",
                                                "agb": "AGB_Mg_ha"},
            "units": "Mg/ha", "csv_text": "Country_Territory,Site,AGB_Mg_ha\nCosta Rica,Nicoya,1\n", **over}


def _query(ds, params):
    tok = identity.bind(OWNER)
    try:
        return feeds.query(ds, params)
    finally:
        identity.unbind(tok)


def test_a_mac_roman_table_lands_counted_by_rows_and_reads_back_accented(gate, log):
    url = "https://figshare.example/files/tabS1.csv"
    gate[url] = MAC_CSV.encode("mac_roman")
    m = {k: v for k, v in _table("test_plots_macroman").items() if k != "csv_text"}
    out = staging.submit("table", {**m, "url": url}, OWNER)
    log("OUTPUT", f"{out['status']} {json.dumps(out.get('preview', {}).get('hints'))}")
    assert out["status"] == "staged", out
    pv = out["preview"]
    assert pv["rows"] == 4 and pv["encoding"] == "mac_roman"        # a multi-line field is one row
    assert "Mac OS Roman" in pv["hints"][0]
    res = _query("test_plots_macroman", {"limit": 10})
    assert res["status"] == "ok" and res["records"][0]["site"] == "Térraba-Sierpe"
    # an empty file says so (it read "file is not readable CSV: " with no reason)...
    gate["https://x.example/empty.csv"] = b""
    bad = staging.submit("table", {**m, "dataset": "test_empty_tbl", "url": "https://x.example/empty.csv"}, OWNER)
    assert bad["status"] == "declined" and "file is empty" in bad["problems"][0]
    # ...and a page is a page
    gate["https://x.example/landing"] = b"<!DOCTYPE html><html><body>dataset page</body></html>"
    page = staging.submit("table", {**m, "dataset": "test_page_tbl", "url": "https://x.example/landing"}, OWNER)
    assert page["status"] == "declined" and "returned a web page, not a CSV file" in page["problems"][0]


def test_a_table_declares_where_its_rows_speak_and_the_row_carries_it(gate, auto, log):
    out = staging.submit("table", _table("test_nicoya_factor", countries="Costa Rica",
                                         bbox=[-85.4, 9.6, -84.6, 10.4]), OWNER)
    assert out["status"] == "approved", out
    row = yaml.safe_load((get_settings().feeds_conf_dir / "test_nicoya_factor.yml").read_text())
    log("OUTPUT", f"countries={row.get('countries')} bbox={row.get('bbox')}")
    assert row["countries"] == ["Costa Rica"] and row["bbox"] == [-85.4, 9.6, -84.6, 10.4]
    assert registry.FEEDS["test_nicoya_factor"]["countries"] == ["Costa Rica"]
    # the land-cover pack's reader skips it for another country (the risk pack reads the same key)
    from app.graph.geo import vectors
    assert not vectors.country_matches(row["countries"], "Ecuador")
    bad = staging.submit("table", _table("test_bad_scope", countries=[3], bbox=[10, 5, -10, 6]), OWNER)
    joined = " ".join(bad["problems"])
    assert "countries must be a list of country names" in joined and "bbox must be" in joined


def test_staged_rows_carry_scope_too(gate, log):
    out = staging.submit("table", _table("test_staged_scope", pack="risk", countries=["Thailand"]), OWNER)
    assert out["status"] == "staged"
    assert staging.STAGED_FEEDS["test_staged_scope"]["countries"] == ["Thailand"]
    assert "bbox" not in staging.STAGED_FEEDS["test_staged_scope"]


def test_feeds_query_filters_a_table_and_declines_what_it_cannot_do(gate, auto, log):
    m = {k: v for k, v in _table("test_plots_filter", csv_text=MAC_CSV).items()}
    assert staging.submit("table", m, OWNER)["status"] == "approved"
    cr = _query("test_plots_filter", {"limit": 5, "country": "costa rica"})
    log("OUTPUT", cr["summary"])
    assert cr["status"] == "ok" and {r["country"] for r in cr["records"]} == {"Costa Rica"}
    assert cr["count"] == 2 and "2 of 4 rows match" in cr["summary"]
    assert cr["passport"]["license"] == "CC-BY-4.0"
    # accent-insensitive value, case-insensitive raw CSV header
    assert _query("test_plots_filter", {"SITE": "Terraba-Sierpe"})["count"] == 1
    assert _query("test_plots_filter", {"Country_Territory": "Panama"})["count"] == 1
    # bbox over the table's own latitude/longitude columns
    gulf = _query("test_plots_filter", {"bbox": [-85.4, 9.6, -84.6, 10.4]})
    assert [r["site"] for r in gulf["records"]] == ["Golfo de Nicoya"]
    # nothing matches -> empty, said
    none = _query("test_plots_filter", {"country": "Kenya"})
    assert none["status"] == "empty" and "no row matches" in none["note"]
    # an unknown parameter is declined with what is accepted
    no = _query("test_plots_filter", {"habitat": "fringe"})
    log("OUTPUT", no["note"])
    assert no["status"] == "declined" and "habitat" in no["note"] and "bbox" in no["note"]
    assert "Country_Territory" in no["note"]


def test_a_table_without_coordinates_declines_a_bbox(gate, auto):
    assert staging.submit("table", _table("test_no_coords"), OWNER)["status"] == "approved"
    res = _query("test_no_coords", {"bbox": [0, 0, 1, 1]})
    assert res["status"] == "declined" and "no latitude/longitude columns" in res["note"]


def test_a_paged_api_says_first_n_of_m(monkeypatch):
    import requests
    from app.mcp import climate_indices
    monkeypatch.setattr(climate_indices, "_CACHE", {})

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"count": 1984, "limit": 300, "results": [{"species": f"s{i}"} for i in range(300)]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    spec = {"title": "GBIF occurrences (test)", "declarative": "test:gbif-total", "cadence": "irregular",
            "fetch": {"url": "https://api.gbif.example/v1/occurrence/search", "records_path": "results",
                      "fields": {"species": "species"}}}
    res = feeds.ADAPTERS["generic_json"]({"limit": 5}, spec)
    assert res["summary"].startswith("first 300 of 1,984 records") and res["upstream_total"] == 1984


# ------------------------------------------------------------- 13. hints
def test_a_hint_points_at_a_pack_that_reaches_the_layer(gate, monkeypatch, log):
    from app.graph.geo import tiffs
    from app.land_cover import synthesis as lc
    monkeypatch.setattr(tiffs, "catalog", lambda include_staged=True: {
        "hazard_flood": {"title": "flood", "extent": {"lon": [92.0, 141.2], "lat": [-11.1, 28.7]}},
        "risk_cr_temp": {"title": "a contributed risk layer"}})
    monkeypatch.setattr(lc, "landcover_stacks", lambda: {})
    # a Thai point layer: the risk pack with a real hazard
    th = staging._where_to_test("th_shelters", [100.4, 13.6, 100.7, 13.9], "vectors", "points")
    assert th.startswith("assemble_pack(pack='risk'") and "hazard='flood'" in th
    # a Costa Rica layer with nothing covering it: said plainly, no risk pack
    cr = staging._where_to_test("cr_points", [-85.9, 8.0, -82.6, 11.2], "vectors", "points")
    log("OUTPUT", cr)
    assert cr.startswith("no pack with an area reaches") and "pack='risk'" not in cr
    # ... and once a land-cover stack covers it, that pack, with a year it has
    monkeypatch.setattr(lc, "landcover_stacks", lambda: {"landcover_cr_gmw": {
        "years": [1996, 2020], "extent": {"lon": [-86.0, -82.5], "lat": [8.0, 11.3]}}})
    raster = staging._where_to_test("risk_cr_temp", [-85.9, 8.0, -82.6, 11.2], "layers")
    assert "pack='land-cover'" in raster and "year=2020" in raster and "layers=['risk_cr_temp']" in raster
    assert "hazard='cr_temp'" not in raster
    # through the gate: a staged vector's preview carries it
    gate["https://x.example/pts.geojson"] = GEOJSON
    out = staging.submit("vector", _vector("cr_test_pts", "CC-BY-4.0"), OWNER)
    log("OUTPUT", out["preview"]["how_to_test"])
    assert "pack='land-cover'" in out["preview"]["how_to_test"] and "flood" not in out["preview"]["how_to_test"]


# ------------------------------------------------------------- 14. audit
def test_the_audit_reconciles_vectors_and_counts_honestly(gate, auto, log):
    gate["https://x.example/pts.geojson"] = GEOJSON
    assert staging.submit("vector", _vector("test_audit_pts", "CC-BY-4.0"), OWNER)["status"] == "approved"
    out = staging.reconcile(OTHER)
    log("OUTPUT", out["note"])
    assert out["serving"] == 1 and out["unverifiable"] == 0
    assert out["note"].startswith("1 of 1 approved served")
    from app.graph.geo import vectors
    vectors.master_path(vectors.landed()["test_audit_pts"]).unlink()
    out = staging.reconcile(OTHER)
    assert out["approved_but_inert"] == 1 and "NOT served" in out["note"]
    assert "missing" in out["contributions"]["inert"][0]["why"]


# ------------------------------------------------------------- 1. hourly cap
def test_the_hourly_cap_is_a_setting_says_when_a_slot_frees_and_0_turns_it_off(gate, monkeypatch, log):
    monkeypatch.setattr(get_settings(), "grp_contrib_hourly_cap", 2)
    gate["https://x.example/pts.geojson"] = GEOJSON
    for name in ("test_cap_a", "test_cap_b"):
        assert staging.submit("vector", _vector(name, "CC-BY-4.0"), OWNER)["status"] == "staged"
    out = staging.submit("vector", _vector("test_cap_c", "CC-BY-4.0"), OWNER)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    assert "2 submissions in the last hour — the cap is 2" in out["problems"][0]
    assert "the next slot frees in about 60 min" in out["problems"][0]
    # per contributor: someone else is not blocked
    assert staging.submit("vector", _vector("test_cap_other", "CC-BY-4.0"), OTHER)["status"] == "staged"
    monkeypatch.setattr(get_settings(), "grp_contrib_hourly_cap", 0)
    assert staging.submit("vector", _vector("test_cap_c", "CC-BY-4.0"), OWNER)["status"] == "staged"
    assert "without an hourly cap" in staging.describe_submit()


def test_a_declined_submission_gives_its_slot_back(gate, monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_contrib_hourly_cap", 1)
    gate["https://x.example/page"] = b"<!DOCTYPE html><html></html>"
    for _ in range(3):
        out = staging.submit("vector", _vector("test_cap_page", "CC-BY-4.0", url="https://x.example/page"), OWNER)
        assert out["status"] == "declined" and "web page" in out["problems"][0]


def test_concurrent_submissions_never_overshoot_the_cap(gate, monkeypatch, log):
    """Check and record are one transaction: ten submissions racing for three slots
    get exactly three — the dry run saw '32 submissions in the last hour — the cap is 30'."""
    monkeypatch.setattr(get_settings(), "grp_contrib_hourly_cap", 3)
    gate["https://x.example/pts.geojson"] = GEOJSON
    store.init()                            # as the server does at boot (WAL on)
    real = staging._KINDS["vector"]["prepare"]

    def slow(m):
        time.sleep(0.3)                     # a fetch: the window the old check raced in
        return real(m)
    monkeypatch.setitem(staging._KINDS["vector"], "prepare", slow)
    results = []

    def go(i):
        results.append(staging.submit("vector", _vector(f"test_race_{i}", "CC-BY-4.0"), OWNER))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = [r for r in results if r["status"] == "staged"]
    capped = [r for r in results if r["status"] == "declined" and "the cap is 3" in r["problems"][0]]
    log("OUTPUT", f"staged={len(ok)} capped={len(capped)}")
    assert len(ok) == 3 and len(capped) == 7
    assert len(store.list_contributions(contributor_id=OWNER.id)) == 3


# ------------------------------------------------------------- 15. description
def test_the_amend_text_names_every_vector_family():
    text = staging.form("amend")["fields"]["required"]["layer"]    # the form, on demand
    assert "a vector layer (points, lines or polygons)" in text
    assert "or a point layer" not in text and "or a point layer" not in staging.describe_submit()
