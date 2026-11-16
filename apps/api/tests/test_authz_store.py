"""The store's health and its outages, against a REAL OpenFGA (skipped without the
binary): /api/health says ok, store-missing or unreachable without creating anything;
a refusal records why for the request; and a pack built while the store is down
declares the gap, whatever the pack, through the seam every pack passes."""
import json
import shutil

import pytest

from app import authz
from app.config import get_settings
from app.contrib import staging
from app.graph.geo import tiffs
from app.mcp import assemble, packs
from test_authz import A2, REVIEWER, _as, _submit, env, fga_server, file_host  # noqa: F401 (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("openfga") is None,
                                reason="the openfga binary is not installed (brew install openfga)")

MISSING = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"       # a well-formed store id no OpenFGA has issued


def _down(monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_fga_url", "http://127.0.0.1:9")
    authz._CLIENT.clear()
    tiffs._BASE.clear()


def test_health_says_ok_with_the_store_up(env, fga_server, log):
    from app.api.routes import health as route
    h = authz.health()
    log("OUTPUT", h)
    assert h == {"store": "ok", "url": fga_server, "store_id": authz.ids()["store_id"], "reason": None}
    assert route.health()["authorization"] == {"store": "ok", "reason": None}    # no address on the public page
    assert authz.outage() is None


def test_health_says_store_missing_when_the_saved_id_names_no_store(env, capsys):
    """Reachable but gone (a memory datastore restarted, a wiped database): the failure the
    ids-keyed client was built for. Health names it and creates nothing: the stale id stays."""
    p = authz._state_path()
    stale = {**json.loads(p.read_text()), "store_id": MISSING}
    p.write_text(json.dumps(stale))
    authz._CLIENT.clear()
    h = authz.health()
    assert h["store"] == "store-missing" and h["store_id"] == MISSING
    assert "replaced or wiped" in h["reason"] and "bootstrap" in h["reason"]
    assert json.loads(p.read_text()) == stale                     # no bootstrap happened
    assert authz.main(["health"]) == 1
    assert json.loads(capsys.readouterr().out)["store"] == "store-missing"
    p.unlink()                                                    # a box that never bootstrapped
    h = authz.health()
    assert h["store"] == "store-missing" and h["store_id"] is None and "no store id is saved" in h["reason"]
    assert not p.exists()


def test_health_says_unreachable_and_a_failed_check_records_the_outage(env, fga_server, monkeypatch, log):
    _down(monkeypatch)
    h = authz.health()
    log("OUTPUT", h)
    assert h["store"] == "unreachable" and "did not answer" in h["reason"]
    assert "127.0.0.1" not in h["reason"]                         # the reason names no address
    assert authz.main(["health"]) == 1
    with pytest.raises(authz.AuthzUnavailable):
        authz.check("user:user_rev", "reviewer", authz.PLATFORM)
    why = authz.outage()
    log("OUTPUT", why)
    assert why and why.startswith("authorization store")
    # The store comes back within the same request: the answer it gives now does not bring
    # back what an earlier reader dropped, so the record of the outage stays.
    monkeypatch.setattr(get_settings(), "grp_fga_url", fga_server)
    authz._CLIENT.clear()
    assert authz.check("user:user_rev", "reviewer", authz.PLATFORM) is True
    assert authz.outage() == why


def test_health_says_off_without_a_url(monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "grp_fga_url", "")
    h = authz.health()
    assert h["store"] == "off" and h["url"] is None and "GRP_FGA_URL" in h["reason"]
    assert authz.main(["health"]) == 1 and json.loads(capsys.readouterr().out) == h
    assert authz.outage() is None


def test_the_shared_gap_seam_speaks_only_once_the_store_failed(env, monkeypatch):
    gaps: list = []
    assemble.shared_gaps(gaps)
    assert gaps == []
    _down(monkeypatch)
    assemble.shared_gaps(gaps)
    assert gaps == []                                            # nothing asked yet: nothing to say
    assert A2.may_view("some-contribution") is False             # the catalog's question, failing closed
    gaps = ["an earlier gap"]
    assemble.shared_gaps(gaps)
    assert gaps[0] == "an earlier gap" and len(gaps) == 2
    assert gaps[1].startswith("authorization store did not answer (") and "contributed layers are hidden" in gaps[1]


def _toy(name: str, seen: dict) -> dict:
    """A contributed pack whose gather reads the raster catalog, as every real pack does."""
    def gather(target, focus, trace, extras):
        seen[name] = sorted(tiffs.catalog())
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [], {"queries": None})
    return {"display_name": name, "version": "v0", "target_keys": ("place",), "target_doc": {"place": "site"},
            "gather": gather, "sections": lambda: ["## Reading"], "corpus": None,
            "default_focus": lambda t: t.get("place", "")}


def test_a_store_that_does_not_answer_is_a_declared_gap_in_every_pack(env, file_host, monkeypatch, log):
    """With the store down the catalog hides every contributed layer (fail closed); the pack
    says so in its gaps, with the reason, for two packs the registry never heard of."""
    up = _submit(file_host, visibility="platform")
    cid = up["contribution_id"]
    assert _as(REVIEWER, lambda: staging.approve(cid, REVIEWER))["status"] == "approved"
    seen = {}
    for name in ("toy-one", "toy-two"):
        monkeypatch.setitem(packs.PACKS, name, _toy(name, seen))
    ok = _as(A2, lambda: assemble.assemble(pack="toy-one", place="Toy bay"))
    assert ok["status"] == "ok" and "hazard_toyheat" in seen["toy-one"]
    assert not any(g.startswith("authorization store") for g in ok["gaps"])
    _down(monkeypatch)
    for name in ("toy-one", "toy-two"):
        authz._OUTAGE.set(None)                                  # each pack learns of the outage itself
        p = _as(A2, lambda: assemble.assemble(pack=name, place="Toy bay"))
        log("OUTPUT", p["gaps"])
        assert p["status"] == "ok" and "hazard_toyheat" not in seen[name]
        lines = [g for g in p["gaps"] if g.startswith("authorization store did not answer (")]
        assert len(lines) == 1 and "contributed layers are hidden" in lines[0]
        declared = next(c for c in p["citations"] if c.get("kind") == "gaps")
        assert lines[0] in declared["text"]                      # the gaps citation carries it too
