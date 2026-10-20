"""A preparation the platform applies BY ITSELF (contrib/prepare: detect, auto) on a
submission that outlives the call (contrib/jobs).

A plain manifest — no preparation field, as a participant's Claude sends it — whose
preparation the platform applies itself and that is still working after the wait answers
`preparing`; contribute_status then returns the landed result carrying
prepared_automatically, with the stored record holding the applied fields. A url whose
shape alone says to prepare it (a FeatureServer query) runs in the preparation lane; a
preparation applied only after a decline (an archive over the cap, read whole) starts in
the plain lane and waits for one of the same preparation slots, so no more than
grp_prep_max_jobs prepare at once, while a submission that prepares nothing never waits.
Beyond the asking pack: the risk pack counts the self-prepared point layer.

Everything is synthetic and local: loopback HTTP servers play the hosts; no test touches
the network."""

import json
import re
import threading
import time

import numpy as np
import pytest

from app.config import get_settings
from app.contrib import jobs, prepare, staging
from app.graph.geo import vectors
from app.mcp import assemble, store

from test_contrib_jobs import _call, _coded, _drain, _json, _keep_checking, _plain, _status, _until, _vuln
from test_prepare_auto import _fields, _layer, _no_leftovers, env  # noqa: F401  (fixture reuse)
from test_prepare_clip import RISK_POLY, _FeatureServer, _admin, _shapefile, _zip
from test_prepare_from_folder import drive  # noqa: F401  (fixture reuse)


@pytest.fixture
def jenv(env, monkeypatch):
    """test_prepare_auto's hermetic platform (auto-approve, the platform's own
    preparation on, loopback hosts), with fresh job lanes, a short wait, the store made
    as the server makes it at boot, and every return of staging.submit recorded."""
    s = get_settings()
    monkeypatch.setattr(s, "grp_prep_max_jobs", 2)
    monkeypatch.setattr(s, "grp_prep_wait_seconds", 0.3)
    monkeypatch.setattr(s, "grp_prep_status_wait_seconds", 0)
    monkeypatch.setattr(jobs, "_lanes", {})
    store.init()
    seen, gates = [], []
    real = staging.submit

    def recording(*a, **k):
        out = real(*a, **k)
        seen.append(out)
        return out
    monkeypatch.setattr(staging, "submit", recording)
    yield {**env, "seen": seen, "gates": gates}
    for g in gates:
        g.set()
    _drain()


def _gated(jenv, fn):
    """A host app answering only once its gate opens."""
    gate = threading.Event()
    jenv["gates"].append(gate)

    def app(path, q):
        gate.wait(60)
        return fn(path, q)
    return app, gate


def _until_step(jid, step, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        out = _status(jid)
        if out.get("step") == step:
            return out
        time.sleep(0.05)
    raise AssertionError(f"job {jid} never reached {step!r}: {_status(jid)}")


def _shelters(n=12):
    return [{"type": "Feature", "id": i + 1,
             "properties": {"OBJECTID": i + 1, "name": f"Shelter {i}"},
             "geometry": {"type": "Point", "coordinates": [103.006 + 0.0007 * i, 13.01]}}
            for i in range(n)]


def test_a_plain_manifest_prepared_by_itself_past_the_wait_lands_through_status(jenv, log):
    """The combined case: a FeatureServer query given plainly is paged whole by the
    platform itself, slower than the wait — `preparing`, then status returns the landed
    result with prepared_automatically, and the record and the layer hold the fields."""
    host = jenv["host"]
    app, gate = _gated(jenv, _FeatureServer(_shelters(), max_records=5))
    url = host.app("/arcgis/rest/services/Shelters/FeatureServer/0", app) \
        + "/query?where=1%3D1&f=geojson"
    m = _layer("toy_shelters", url, name_field="name", countries=["Toyland"])
    assert not prepare.requested("vector", m)                 # plain: no preparation field
    out = _call("contribute_submit", {"kind": "vector", "manifest": m})
    log("OUTPUT", json.dumps(out))
    assert out["status"] == "preparing" and out["kind"] == "vector"
    jid = out["contribution_id"]
    assert re.fullmatch(r"[0-9a-f]{16}", jid) and store.load_contribution(jid) is None
    assert jobs._row(jid)["lane"] == "prepare"                # the url's shape sorted it
    mid = _until_step(jid, "the clip step")
    assert re.fullmatch(r"the platform is still preparing this \(the clip step, \d+ s so far\); "
                        + _keep_checking(jid), mid["message"])
    gate.set()
    final = _until(jid)
    log("OUTPUT", json.dumps(final)[:400])
    assert final["status"] == "approved", final
    assert final == _json(jenv["seen"][-1])                   # exactly what submit returned
    assert _call("contribute_status", {"contribution_id": jid}) == final
    assert _fields(final) == [{"whole": True}]
    assert final["message"].startswith("The platform prepared this contribution by itself — ")
    assert "an ArcGIS FeatureServer/MapServer layer" in final["prepared_automatically"][0]["why"]
    # the stored fields: the record's manifest holds them, marked as the platform's
    real = final["contribution_id"]
    assert real != jid
    rec = store.load_contribution(real)
    assert rec["status"] == "approved" and rec["manifest"]["whole"] is True
    assert rec["manifest"]["url"] == url and "whole" not in m
    assert rec["prepared_automatically"] == final["prepared_automatically"]
    shown = _call("contribute_status", {"contribution_id": real})["contribution"]
    assert shown["prepared_automatically"] == final["prepared_automatically"]
    row = vectors.landed()["toy_shelters"]
    assert row["features"] == 12 and row["fetched"]["steps"][0]["inputs"]["whole"] is True
    assert row["fetched"]["prepared_automatically"] == [
        {"fields": p["fields"], "why": p["why"]} for p in final["prepared_automatically"]]
    # beyond the asking pack: the risk pack counts the layer that prepared itself
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY)
    assert p["status"] == "ok", p.get("note")
    exp = next(c for c in p["citations"]
               if c.get("title", "").startswith("toy_shelters (synthetic) vs"))
    assert "contributed layer" in exp["text"]
    assert _no_leftovers(jenv)


def test_a_preparation_applied_after_a_decline_waits_for_a_free_preparation_slot(
        jenv, monkeypatch, log):
    """One slot. A declared recode holds it; HDX's all-levels zip, over the cap as a
    download, runs in the plain lane, is declined at the cap, gets whole: true by itself —
    and waits for the slot before the whole read. A FeatureServer query queues in the
    preparation lane with its position; a submission preparing nothing lands at once."""
    s = get_settings()
    monkeypatch.setattr(s, "grp_prep_max_jobs", 1)
    host = jenv["host"]
    app, gate = _gated(jenv, lambda path, q: (200, "application/octet-stream", _coded()))
    first = _call("contribute_submit", {"kind": "raster",
                                        "manifest": _vuln(host.app("/slow_coded.tif", app))})
    assert first["status"] == "preparing"
    _until_step(first["contribution_id"], "fetching the source")

    levels = _admin()
    members = {}
    for stem, (g, p) in levels.items():
        members.update({f"cri_admin_boundaries/{k}": v for k, v in _shapefile(stem, g, p).items()})
    members["cri_admin_boundaries/cod-ab-cri.pdf"] = np.random.default_rng(1).bytes(300_000)
    raw = _zip(members)
    arch = host.put("cri_admin_boundaries.shp.zip", raw)
    monkeypatch.setattr(s, "grp_contrib_max_bytes", 100_000)
    assert len(raw) > 100_000
    m = _layer("cr_admin2", arch, layer_name="cri_admin2", name_field="adm2_name")
    assert jobs._sorted("vector", m) == ("vector", "plain")    # nothing in its url says so
    second = _call("contribute_submit", {"kind": "vector", "manifest": m})
    assert second["status"] == "preparing"
    sid = second["contribution_id"]
    mid = _until_step(sid, jobs.SLOT_STEP)
    log("OUTPUT", json.dumps(mid))
    assert "queue_position" not in mid
    assert re.fullmatch(r"the platform is still preparing this \(waiting for a free slot to "
                        r"prepare it in, \d+ s so far\); " + _keep_checking(sid),
                        mid["message"])
    path = "/cri_admin_boundaries.shp.zip"
    asked = len(host.asked(path))
    assert asked >= 1                                         # the attempt declined at the cap
    time.sleep(0.3)
    assert len(host.asked(path)) == asked                     # the whole read waits

    url = host.app("/arcgis/rest/services/Shelters/FeatureServer/0",
                   _FeatureServer(_shelters(), max_records=5)) + "/query?where=1%3D1&f=geojson"
    third = _call("contribute_submit", {"kind": "vector", "manifest": _layer(
        "toy_shelters", url, name_field="name", countries=["Toyland"])})
    assert third["status"] == "preparing" and third["queue_position"] == 1

    pm, praw = _plain(host.put("plain.tif", b""))
    host.files["/plain.tif"] = praw
    monkeypatch.setattr(s, "grp_prep_wait_seconds", 30)
    assert _call("contribute_submit", {"kind": "raster", "manifest": pm})["status"] == "approved"

    gate.set()
    assert _until(first["contribution_id"])["status"] == "approved"
    final = _until(sid)
    log("OUTPUT", json.dumps(final)[:400])
    assert final["status"] == "approved", final
    assert _fields(final) == [{"whole": True}]
    assert len(host.asked(path)) > asked                      # read whole once the slot was its own
    assert vectors.landed()["cr_admin2"]["features"] == 24
    assert store.load_contribution(final["contribution_id"])["manifest"]["whole"] is True
    last = _until(third["contribution_id"])
    assert last["status"] == "approved" and _fields(last) == [{"whole": True}]
    assert vectors.landed()["toy_shelters"]["features"] == 12
    # every slot is back, and the hook lives only in a job's own context
    slots = jobs._lane("prepare").slots
    assert slots.acquire(blocking=False)
    slots.release()
    assert prepare.GATE.get() is None
    assert _no_leftovers(jenv)


def test_a_submission_failing_its_checks_is_declined_at_once_beside_a_full_lane(
        jenv, monkeypatch, log):
    """The acceptance run's case: a folder-shaped url sorts to the preparation lane, and a
    manifest missing a required field waited 99 s behind other preparations to be declined
    in 0.0 s. Now a submission failing its fetch-free checks is declined in the call."""
    monkeypatch.setattr(get_settings(), "grp_prep_max_jobs", 1)
    host = jenv["host"]
    app, gate = _gated(jenv, _FeatureServer(_shelters(), max_records=5))
    url = host.app("/arcgis/rest/services/Busy/FeatureServer/0", app) + "/query?where=1%3D1&f=geojson"
    busy = _call("contribute_submit", {"kind": "vector",
                                       "manifest": _layer("toy_busy", url, countries=["Toyland"])})
    assert busy["status"] == "preparing"                       # the one slot is taken
    m = _layer("toy_missing", url, countries=["Toyland"])
    del m["layer"]
    t0 = time.monotonic()
    out = _call("contribute_submit", {"kind": "vector", "manifest": m})
    log("OUTPUT", json.dumps(out)[:300])
    assert out["status"] == "declined" and time.monotonic() - t0 < 5
    assert any("layer" in p for p in out["problems"])
    gate.set()
    assert _until(busy["contribution_id"])["status"] == "approved"


def test_status_waits_for_a_job_that_finishes_within_its_wait(jenv, monkeypatch):
    """An agent asking again gets the result instead of polling every few seconds."""
    monkeypatch.setattr(get_settings(), "grp_prep_status_wait_seconds", 30)
    host = jenv["host"]
    app, gate = _gated(jenv, _FeatureServer(_shelters(), max_records=5))
    url = host.app("/arcgis/rest/services/Slow/FeatureServer/0", app) + "/query?where=1%3D1&f=geojson"
    out = _call("contribute_submit", {"kind": "vector",
                                      "manifest": _layer("toy_slow", url, countries=["Toyland"])})
    assert out["status"] == "preparing"
    assert "each check waits up to 30 s" in out["message"]
    threading.Timer(1.0, gate.set).start()
    t0 = time.monotonic()
    final = _call("contribute_status", {"contribution_id": out["contribution_id"]})
    assert final["status"] == "approved" and 0.5 < time.monotonic() - t0 < 25
