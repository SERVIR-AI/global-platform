"""Reviewers see who submitted an item. A sign-in token's name and e-mail are held apart from the
label every record carries (the label stays the account id, because records reach public answers),
kept only for people who contribute (contrib/people.py) and shown only to reviewers."""
import json

import pytest

from app import authz
from app.config import get_settings
from app.contrib import identity, people, staging
from app.mcp import store

REVIEWER = identity.Caller("user_rev", "user_rev", True, "bound")
STRANGER = identity.Caller("user_v", "user_v", False, "bound")
TABLE = {"kind": "table", "dataset": "people_test_table", "title": "People test table",
         "description": "A tiny depth-damage table submitted in a test.", "source": "test fixture",
         "validation": "unvalidated", "license": "CC0-1.0", "vintage": "2026-09", "cadence": "irregular",
         "columns": {"depth_m": "Depth_m", "residential": "Residential"},
         "units": "fraction of replacement cost", "pack": "risk",
         "csv_text": "Depth_m,Residential\n0.5,0.25\n1.0,0.40\n"}


@pytest.fixture
def db(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "feeds_conf_dir", tmp_path / "feeds", raising=False)
    (tmp_path / "feeds").mkdir()
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "grp.db")
    monkeypatch.setattr(people, "_HELD", {})
    store.init()


def _signed_in(monkeypatch, **claims):
    """The caller behind the MCP transport's verified token, as fastmcp hands it over."""
    import fastmcp.server.dependencies as deps

    class Token:
        pass
    token = Token()
    token.claims = claims
    monkeypatch.setattr(deps, "get_access_token", lambda: token)
    return identity.resolve()


def _submitted(contributor_id, kind, title, **extra):
    return store.save_contribution({"status": "pending", "kind": kind, "title": title,
                                    "contributor_id": contributor_id,
                                    "contributor_label": contributor_id, **extra})


def _contributor(user_id, email=None, name=None):
    people.seen(user_id, email, name)
    people.keep(user_id)


def test_a_signed_in_callers_label_stays_the_account_id_and_nothing_is_kept_until_they_contribute(db, monkeypatch):
    caller = _signed_in(monkeypatch, sub="user_t1", email="t1@hub.test", name="T One")
    assert (caller.id, caller.label) == ("user_t1", "user_t1")
    assert store.load_people(["user_t1"]) == {} and people.describe("user_t1") == "user_t1"


def test_a_submission_keeps_who_sent_it_and_its_record_carries_only_the_id(db, monkeypatch):
    caller = _signed_in(monkeypatch, sub="user_t2", email="t2@hub.test", name="T Two")
    out = staging.submit("table", dict(TABLE), caller=caller)
    rec = store.load_contribution(out["contribution_id"])
    assert rec["contributor_label"] == "user_t2" and "t2@hub.test" not in json.dumps(rec)
    assert people.describe("user_t2") == "T Two <t2@hub.test>"


def test_a_store_that_fails_never_fails_the_call(db, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(store, "save_person", broken)
    caller = _signed_in(monkeypatch, sub="user_t3", email="t3@hub.test")
    people.keep(caller.id)
    assert caller.id == "user_t3" and people.describe("user_t3") == "user_t3"


def test_the_review_queue_names_who_submitted_each_item_of_any_pack(db):
    doc = _submitted("user_old", "document", "Rice yield report", manifest={"pack": "food-security"})
    tif = _submitted("user_old", "raster", "Monthly maximum temperature", manifest={"layer": "measure_tmax"})
    _submitted("user_new", "raster", "Mangrove extent", manifest={"layer": "landcover_mangrove"})
    _contributor("user_old", "olga@hub.test", "Olga Old")

    rows = {r["contribution_id"]: r for r in staging.review_list(REVIEWER)["contributions"]}
    for cid in (doc, tif):
        assert rows[cid]["contributor"] == "Olga Old <olga@hub.test>" and rows[cid]["contributor_id"] == "user_old"
    nobody = [r for r in rows.values() if r["contributor_id"] == "user_new"]
    assert nobody and nobody[0]["contributor"] == "user_new"
    queue = staging.status(caller=REVIEWER)["pending_review"]
    assert {q["contributor"] for q in queue} == {"Olga Old <olga@hub.test>", "user_new"}
    assert staging.review_list(STRANGER)["status"] != "ok"


def test_the_reviewers_pending_list_stays_inside_its_budget_with_names(db):
    for n in range(30):
        uid = f"user_{n:02d}"
        _submitted(uid, "document", f"Report {n}", manifest={"pack": "food-security"})
        _contributor(uid, f"firstname.lastname{n:02d}@organisation-hub.example.org",
                     f"Firstname{n:02d} Lastname-Longer")
    queue = staging.status(caller=REVIEWER)["pending_review"]
    assert queue and all("Lastname-Longer" in q["contributor"] for q in queue)
    assert len(json.dumps(queue)) <= staging.STATUS_CHARS // 2 + 2 * len(queue)


def test_hub_lookups_stop_at_the_first_store_failure_and_say_so(db, monkeypatch):
    for uid in ("user_a", "user_b", "user_c"):
        _submitted(uid, "document", f"Report by {uid}", manifest={"pack": "food-security"})
    asked = []

    def unavailable(caller):
        asked.append(caller.id)
        raise authz.AuthzUnavailable("timed out")
    monkeypatch.setattr(authz, "enabled", lambda: True)
    monkeypatch.setattr(authz, "hubs_of", unavailable)
    reply = staging.review_list(REVIEWER)
    assert len(asked) == 1 and "from there on are not shown" in reply["note"]
    assert len(reply["contributions"]) == 3


def test_sharing_details_name_people_for_reviewers_only(db, monkeypatch):
    cid = _submitted("user_owner", "raster", "Mangrove extent", manifest={"layer": "landcover_mangrove"})
    _contributor("user_owner", "olga@hub.test", "Olga Old")
    _contributor("user_b", "bea@hub.test")
    monkeypatch.setattr(authz, "enabled", lambda: True)
    monkeypatch.setattr(authz, "outage", lambda: None)
    monkeypatch.setattr(identity, "_visible_for", lambda caller: {cid})      # a public dataset
    monkeypatch.setattr(authz, "sharing", lambda c: {
        "contribution_id": c, "visibility": "public", "owner": ["user_owner"], "hub": [],
        "shared_users": ["user_b"], "shared_hubs": [], "approved": True})

    assert staging.share(cid, "show", caller=REVIEWER)["sharing"]["people"] == {
        "user_owner": "Olga Old <olga@hub.test>", "user_b": "bea@hub.test"}
    # the owner chose whom to share with by id, so names for them would turn any id into an e-mail
    owner = identity.Caller("user_owner", "user_owner", False, "bound")
    anonymous = identity.Caller(identity.ANONYMOUS, identity.ANONYMOUS, False, "anonymous")
    for who in (owner, STRANGER, anonymous):
        reply = staging.share(cid, "show", caller=who)
        assert reply["status"] == "ok" and "people" not in reply["sharing"] and "@" not in json.dumps(reply)


def test_names_are_cleaned_and_a_cleared_name_clears(db):
    rlo, lt, gt = chr(0x202E), chr(0xFF1C), chr(0xFF1E)            # a bidi override, look-alike brackets
    selectors = chr(0xFE0F) + chr(0xE0101)
    _contributor("user_n", "n@hub.test", "Olga Old <olga@hub.test>" + rlo + chr(10) + "x" * 5000)
    shown = people.describe("user_n")
    assert "<olga" not in shown and rlo not in shown and chr(10) not in shown and len(shown) <= 140
    _contributor("user_k", "k@hub.test", "Kim " + lt + "kim@evil.test" + gt + " X" + selectors)
    assert people.describe("user_k") == "Kim kimevil.test X <k@hub.test>"
    _contributor("user_n", "n@hub.test", " ")                    # the template renders no name
    assert people.describe("user_n") == "n@hub.test"
    _contributor("user_l", ["l@hub.test"], "Lee")                 # a claim that is not a string
    assert people.describe("user_l") == "Lee"
    _contributor("user_e", "not an address", None)
    assert people.describe("user_e") == "user_e"

def test_the_operator_records_and_forgets_a_person(db, capsys):
    assert people.main(["set", "user_z", "--email", "zoe@hub.test", "--name", "Zoe Quinn"]) == 0
    assert json.loads(capsys.readouterr().out) == {"user_z": "Zoe Quinn <zoe@hub.test>"}
    people.seen("user_z", "zoe@hub.test", "Zoe Q.")              # a later token renames her
    people.keep("user_z")
    assert people.describe("user_z") == "Zoe Q. <zoe@hub.test>"
    assert people.main(["forget", "user_z"]) == 0 and people.describe("user_z") == "user_z"
    with pytest.raises(SystemExit):
        people.main(["set", "user_w"])
    with pytest.raises(SystemExit):
        people.main(["set", "user_w", "--email", "not an address"])


def test_a_returning_contributor_is_kept_when_they_read_their_own_contributions(db, monkeypatch):
    _submitted("user_r", "document", "An older report", manifest={"pack": "food-security"})
    caller = _signed_in(monkeypatch, sub="user_r", email="r@hub.test", name="Ray Return")
    assert staging.status(caller=caller)["status"] == "ok"
    assert people.describe("user_r") == "Ray Return <r@hub.test>"


def test_share_show_gives_no_names_to_a_user_the_item_is_shared_with(db, monkeypatch):
    cid = _submitted("user_owner", "raster", "Mangrove extent", manifest={"layer": "landcover_mangrove"})
    _contributor("user_owner", "olga@hub.test", "Olga Old")
    monkeypatch.setattr(authz, "enabled", lambda: True)
    monkeypatch.setattr(authz, "outage", lambda: None)
    monkeypatch.setattr(identity, "_visible_for", lambda caller: {cid})
    monkeypatch.setattr(authz, "sharing", lambda c: {
        "contribution_id": c, "visibility": "private", "owner": ["user_owner"], "hub": [],
        "shared_users": ["user_b"], "shared_hubs": [], "approved": True})
    shared_with = identity.Caller("user_b", "user_b", False, "bound")
    reply = staging.share(cid, "show", caller=shared_with)
    assert reply["status"] == "ok" and "people" not in reply["sharing"] and "@" not in json.dumps(reply)


def test_hubs_are_shown_when_the_store_answers_and_not_asked_when_it_is_off(db, monkeypatch):
    _submitted("user_h", "document", "A hub report", manifest={"pack": "food-security"})
    monkeypatch.setattr(authz, "enabled", lambda: True)
    monkeypatch.setattr(authz, "hubs_of", lambda caller: ["mekong"] if caller.id == "user_h" else [])
    row = staging.review_list(REVIEWER)["contributions"][0]
    assert row["contributor_hubs"] == ["mekong"]

    def never(caller):
        raise AssertionError("asked the store while it is off")
    monkeypatch.setattr(authz, "enabled", lambda: False)
    monkeypatch.setattr(authz, "hubs_of", never)
    reply = staging.review_list(REVIEWER)
    assert "contributor_hubs" not in reply["contributions"][0] and "note" not in reply


def test_nothing_is_kept_for_a_declined_submission_or_a_caller_without_contributions(db, monkeypatch):
    caller = _signed_in(monkeypatch, sub="user_d", email="d@hub.test", name="Dee Decline")
    declined = staging.submit("table", {**TABLE, "csv_text": ""}, caller=caller)
    assert declined["status"] == "declined"
    assert staging.status(caller=caller)["status"] == "ok"
    assert store.load_people(["user_d"]) == {}


def test_a_later_token_without_an_email_keeps_the_stored_email(db):
    _contributor("user_m", "m@hub.test", "Mo Mail")
    _contributor("user_m", None, "Mo Renamed")
    assert people.describe("user_m") == "Mo Renamed <m@hub.test>"


def test_a_reviewer_listed_by_email_is_still_a_reviewer_with_the_store_off(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_reviewers", ["rev@hub.test"])
    monkeypatch.setattr(authz, "enabled", lambda: False)
    caller = _signed_in(monkeypatch, sub="user_rev2", email="rev@hub.test")
    assert caller.label == "user_rev2" and caller.is_reviewer
    other = _signed_in(monkeypatch, sub="user_x2", email="x2@hub.test")
    assert not other.is_reviewer


def test_reading_ones_own_contribution_by_id_keeps_the_reader(db, monkeypatch):
    cid = _submitted("user_r3", "document", "An older report", manifest={"pack": "food-security"})
    caller = _signed_in(monkeypatch, sub="user_r3", email="r3@hub.test", name="Rae Three")
    assert staging.status(cid, caller=caller)["status"] == "ok"
    assert people.describe("user_r3") == "Rae Three <r3@hub.test>"


def test_an_operator_name_survives_a_token_without_one_and_forget_takes_effect_at_once(db, capsys):
    assert people.main(["set", "user_o", "--email", "o@hub.test", "--name", "Oli Operator"]) == 0
    _contributor("user_o", "o@hub.test", None)                 # the template renders no name
    assert people.describe("user_o") == "Oli Operator <o@hub.test>"
    _contributor("user_o", "o@hub.test", "Oli Token")          # a token's name replaces it
    assert people.describe("user_o") == "Oli Token <o@hub.test>"
    assert people.main(["forget", "user_o"]) == 0 and people.describe("user_o") == "user_o"
    people.keep("user_o")                                       # the same token, as the server holds it
    assert people.describe("user_o") == "Oli Token <o@hub.test>"
    capsys.readouterr()


def test_missing_lists_the_contributors_no_name_is_known_for(db, capsys):
    for n in range(3):
        _submitted("user_many", "document", f"Report {n}", manifest={"pack": "food-security"})
    _submitted("user_one", "raster", "A map", manifest={"layer": "measure_one"})
    _submitted("user_known", "raster", "Another map", manifest={"layer": "measure_two"})
    _submitted(identity.LOCAL_DEV, "raster", "A local test", manifest={"layer": "measure_three"})
    _contributor("user_known", "known@hub.test", "Kay Known")
    assert people.main(["missing"]) == 0
    assert json.loads(capsys.readouterr().out) == {"user_many": 3, "user_one": 1}


def test_import_records_many_people_and_says_what_it_skipped(db, capsys):
    raw = json.dumps({"user_a1": {"email": "a1@hub.test", "name": "Ana One"},
                      "user_a2": {"email": "a2@hub.test"},
                      "user_bad": {"email": "not an address", "name": "Bad Mail"},
                      "user_none": {}})
    assert people.main(["import", raw]) == 1                       # something was skipped
    out = json.loads(capsys.readouterr().out)
    assert out["user_a1"] == "Ana One <a1@hub.test>" and out["user_a2"] == "a2@hub.test"
    assert out["user_bad"].startswith("skipped") and out["user_none"].startswith("skipped")
    assert store.load_people(["user_bad", "user_none"]) == {}
    _contributor("user_a1", "a1@hub.test", None)                    # an operator's name outlives a nameless token
    assert people.describe("user_a1") == "Ana One <a1@hub.test>"
    with pytest.raises(SystemExit):
        people.main(["import", "not json"])
    with pytest.raises(SystemExit):
        people.main(["import", "[1, 2]"])
