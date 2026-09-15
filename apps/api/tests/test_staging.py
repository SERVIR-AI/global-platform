"""X4.1 — contribute a document over the MCP: refused loudly or staged; a staged
document is visible to its contributor and reviewers only; approval makes it
public; rejection and withdrawal remove the preview and keep the archive."""
import hashlib

import numpy as np
import pytest

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.mcp import fetch, registry, store
from app.rag.embed import ProviderEmbedder
from app.rag.store import Corpus

OWNER = identity.Caller("user_owner", "hydrologist@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "analyst@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")

TEXT = ("El Nino conditions strengthened during August 2026. Sea surface temperature "
        "anomalies exceeded plus three degrees in the eastern Pacific. The outlook favours "
        "a very strong event through the winter. Maize in Kenya faces above-normal short "
        "rains risk.")


class HashEmbedder:
    def embed(self, texts):
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha1(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).standard_normal(64).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "grp_mattermost_webhook", "")
    monkeypatch.setattr(get_settings(), "rag_min_relevance", -1.0)  # hash embedder: no real scores
    monkeypatch.setattr(ProviderEmbedder, "embed", lambda self, texts: HashEmbedder().embed(texts))
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (TEXT.encode(), "bulletin.txt"))
    return tmp_path


def _manifest(**over):
    d = {"pack": "food-security", "url": "https://example.org/enso/sep2026.txt",
         "source": "NOAA CPC", "title": "ENSO Diagnostic Discussion Sep 2026",
         "pub_date": "2026-09", "temporal": "forecast", "validation": "single-agency",
         "usage_notes": "September 2026 snapshot only."}
    d.update(over)
    return d


def _as(caller):
    return identity.bind(caller)


def test_problems_are_named_at_once_and_nothing_is_stored(env, log):
    out = staging.submit("document", {"pack": "food-security", "file": "/tmp/x.pdf",
                                      "temporal": "someday"}, OWNER)
    log("OUTPUT", str(out["problems"]))
    assert out["status"] == "declined"
    joined = " ".join(out["problems"])
    assert "'file' is not accepted" in joined and "missing required field 'url'" in joined
    assert "temporal must be one of" in joined and "fields" in out
    assert store.list_contributions() == []


def test_unknown_kind_names_what_is_possible(env, log):
    out = staging.submit("pack", {}, OWNER)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and "document" in out["problems"][0]


def test_staged_document_is_visible_to_owner_and_reviewer_only(env, log):
    out = staging.submit("document", _manifest(), OWNER)
    log("OUTPUT", f"{out['status']} {out['contribution_id']} preview={out['preview']['doc_id']}")
    assert out["status"] == "staged" and out["preview"]["chunks"] >= 1
    doc_id = out["preview"]["doc_id"]

    for caller, sees in ((OWNER, True), (OTHER, False), (REVIEWER, True)):
        tok = _as(caller)
        try:
            hits = fetch.search("El Nino outlook Kenya maize", k=5)
            ids = {h["passport"]["doc_id"] for h in hits.get("hits", [])}
            inv = fetch.document()
            listed = {d["doc_id"] for d in inv["inventory"]}
            one = fetch.document(doc_id)
        finally:
            identity.unbind(tok)
        log("OUTPUT", f"{caller.label}: search={doc_id in ids} inventory={doc_id in listed} "
                      f"by-id={one['status']}")
        assert (doc_id in ids) is sees
        assert (doc_id in listed) is sees
        assert (one["status"] == "ok") is sees
        if sees:
            assert one["passport"]["staged"]["contribution_id"] == out["contribution_id"]
            assert "STAGED" in one["passport"]["staged"]["note"]


def test_owner_sees_their_status_other_does_not(env, log):
    out = staging.submit("document", _manifest(), OWNER)
    cid = out["contribution_id"]
    mine = staging.status(None, OWNER)
    assert [c["contribution_id"] for c in mine["contributions"]] == [cid]
    assert staging.status(cid, OTHER)["status"] == "declined"
    assert staging.status(cid, REVIEWER)["contribution"]["status"] == "pending"
    queue = staging.status(None, REVIEWER)["pending_review"]
    log("OUTPUT", f"reviewer queue: {[q['contribution_id'] for q in queue]}")
    assert queue[0]["contribution_id"] == cid


def test_approval_lands_the_same_doc_id_for_everyone(env, log):
    out = staging.submit("document", _manifest(), OWNER)
    cid, doc_id = out["contribution_id"], out["preview"]["doc_id"]
    assert staging.approve(cid, OTHER)["status"] == "declined"          # not a reviewer
    ok = staging.approve(cid, REVIEWER, "looks right")
    log("OUTPUT", f"{ok['status']} landing={ok['landing']}")
    assert ok["status"] == "approved" and ok["landing"]["doc_id"] == doc_id
    tok = _as(OTHER)
    try:
        one = fetch.document(doc_id)
    finally:
        identity.unbind(tok)
    assert one["status"] == "ok" and "staged" not in one["passport"]
    assert "staged_by" not in Corpus("food-security").find(doc_id)["metadata"]
    assert staging.approve(cid, REVIEWER)["status"] == "declined"       # not pending any more


def test_rejection_removes_the_preview_and_keeps_the_archive(env, log):
    out = staging.submit("document", _manifest(), OWNER)
    cid, doc_id = out["contribution_id"], out["preview"]["doc_id"]
    assert staging.reject(cid, "", REVIEWER)["status"] == "declined"   # a note is required
    rej = staging.reject(cid, "wrong pack — this is a risk bulletin", REVIEWER)
    log("OUTPUT", f"{rej['status']} retired={rej['retired']}")
    assert rej["status"] == "rejected" and rej["retired"]["removed"]
    corpus = Corpus("food-security")
    assert corpus.find(doc_id) is None and corpus.raw_path(doc_id) is not None
    assert staging.status(cid, OWNER)["contribution"]["decision_note"].startswith("wrong pack")


def test_withdraw_is_the_owners_alone(env, log):
    out = staging.submit("document", _manifest(), OWNER)
    cid, doc_id = out["contribution_id"], out["preview"]["doc_id"]
    assert staging.withdraw(cid, OTHER)["status"] == "declined"
    w = staging.withdraw(cid, OWNER)
    log("OUTPUT", f"{w['status']} retired={w['retired']['removed']}")
    assert w["status"] == "withdrawn" and Corpus("food-security").find(doc_id) is None


def test_a_live_document_cannot_be_contributed_again(env, log):
    out = staging.submit("document", _manifest(), OWNER)
    staging.approve(out["contribution_id"], REVIEWER)
    again = staging.submit("document", _manifest(title="same bytes, new title"), OTHER)
    log("OUTPUT", again["problems"][0])
    assert again["status"] == "declined" and "already in the food-security library" in again["problems"][0]
    assert store.load_contribution(again["contribution_id"])["status"] == "failed"


def test_someone_elses_staged_document_is_declined_without_revealing_it(env, log):
    staging.submit("document", _manifest(), OWNER)
    again = staging.submit("document", _manifest(), OTHER)
    log("OUTPUT", again["problems"][0])
    assert again["status"] == "declined" and "another contributor" in again["problems"][0]


def test_fetch_refusals_become_declines(env, monkeypatch, log):
    def refuse(url, max_bytes=None):
        raise fetch_policy.FetchRefused("host resolves to 10.1.30.110, a private address")
    monkeypatch.setattr(fetch_policy, "fetch", refuse)
    out = staging.submit("document", _manifest(), OWNER)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and "private address" in out["problems"][0]


def test_pending_cap_is_enforced(env, monkeypatch, log):
    monkeypatch.setattr(staging, "PENDING_CAP", 2)
    for i in range(2):
        r = staging.submit("document", _manifest(url=f"https://example.org/{i}.txt"), OWNER)
        monkeypatch.setattr(fetch_policy, "fetch",
                            lambda url, max_bytes=None, i=i: (f"{TEXT} v{i + 1}".encode(), "b.txt"))
        assert r["status"] == "staged", r
    third = staging.submit("document", _manifest(url="https://example.org/3.txt"), OWNER)
    log("OUTPUT", third["problems"][0])
    assert third["status"] == "declined" and "awaiting review" in third["problems"][0]


def test_coverage_and_capabilities_ignore_previews(env, log):
    staging.submit("document", _manifest(countries=["Kenya"], crops=["maize"]), OWNER)
    cov = registry.coverage()
    log("OUTPUT", f"coverage countries={cov['countries']} crops={cov['crops']}")
    assert "Kenya" not in cov["countries"] and "maize" not in cov["crops"]


def test_contribute_bone_is_now_available(log):
    import asyncio
    from app.mcp.server import mcp
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    bones = {b["bone"]: b["status"] for b in registry._bones(names)}
    log("OUTPUT", f"contribute bone: {bones['contribute']}")
    assert bones["contribute"] == "available"


def test_url_policy_refuses_private_hosts_and_bad_schemes(log):
    problems = fetch_policy.check_url("http://127.0.0.1:8080/x")
    assert problems and "loopback" in problems[0] or "private" in problems[0]
    assert fetch_policy.check_url("ftp://example.org/x")[0].startswith("url scheme")
    assert fetch_policy.check_url("http://user:pw@example.org/x")
    assert fetch_policy.check_url("http://10.1.30.110/x")
    log("OUTPUT", "loopback, private, ftp and embedded credentials all refused")
