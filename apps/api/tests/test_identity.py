"""X4.0 — who is calling: token subject first, dev header only with OAuth off,
reviewer allowlist, and the visibility rule itself."""
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.contrib import identity


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "grp_oauth_enabled", False)
    monkeypatch.setattr(s, "grp_reviewers", [])
    return s


def _token(sub, email=None):
    return SimpleNamespace(subject=sub, client_id="c", claims={"sub": sub, **({"email": email} if email else {})})


def test_token_subject_wins_and_email_is_the_label(settings, monkeypatch, log):
    monkeypatch.setattr(identity, "_from_token", lambda: ("user_01ABC", "sci@hub.org"))
    monkeypatch.setattr(identity, "_from_dev_header", lambda: "spoof@evil")
    c = identity.resolve()
    log("OUTPUT", f"{c}")
    assert (c.id, c.label, c.source) == ("user_01ABC", "sci@hub.org", "token")


def test_dev_header_only_counts_with_oauth_off(settings, monkeypatch, log):
    monkeypatch.setattr(identity, "_from_token", lambda: None)
    monkeypatch.setattr(identity, "_from_dev_header", lambda: "alice@hub.test")
    c = identity.resolve()
    log("OUTPUT", f"oauth off -> {c.id} via {c.source}")
    assert (c.id, c.source) == ("alice@hub.test", "dev-header")
    monkeypatch.setattr(settings, "grp_oauth_enabled", True)
    c = identity.resolve()
    log("OUTPUT", f"oauth on, same header -> {c.id} via {c.source}")
    assert c.source == "anonymous" and not c.is_reviewer


def test_local_dev_reviews_only_while_the_list_is_empty(settings, monkeypatch, log):
    monkeypatch.setattr(identity, "_from_token", lambda: None)
    monkeypatch.setattr(identity, "_from_dev_header", lambda: None)
    assert identity.resolve() == identity.Caller("local-dev", "local-dev", True, "local-dev")
    monkeypatch.setattr(settings, "grp_reviewers", ["reviewer@hub.test"])
    c = identity.resolve()
    log("OUTPUT", f"list set -> local-dev reviewer? {c.is_reviewer}")
    assert not c.is_reviewer


def test_reviewer_list_matches_id_or_label_case_insensitively(settings, monkeypatch, log):
    monkeypatch.setattr(settings, "grp_reviewers", ["Reviewer@Hub.test", "user_01REV"])
    monkeypatch.setattr(identity, "_from_token", lambda: ("user_01REV", "user_01REV"))
    assert identity.resolve().is_reviewer
    monkeypatch.setattr(identity, "_from_token", lambda: ("user_01X", "reviewer@hub.test"))
    assert identity.resolve().is_reviewer
    monkeypatch.setattr(identity, "_from_token", lambda: ("user_01Y", "someone@hub.test"))
    c = identity.resolve()
    log("OUTPUT", f"unlisted -> reviewer? {c.is_reviewer}")
    assert not c.is_reviewer


def test_visibility_rule_owner_reviewer_nobody_else(log):
    owner = identity.Caller("a", "a", False, "bound")
    other = identity.Caller("b", "b", False, "bound")
    reviewer = identity.Caller("r", "r", True, "bound")
    assert owner.may_see("a") and reviewer.may_see("a") and not other.may_see("a")
    assert other.may_see(None)  # unstaged is public
    token = identity.bind(other)
    try:
        assert not identity.visible({"staged_by": "a"})
        assert identity.visible({"title": "live doc"})
    finally:
        identity.unbind(token)
    log("OUTPUT", "owner/reviewer see staged; other does not; unstaged public")


def test_settings_parse_reviewer_lists(log):
    from app.config import Settings
    assert Settings._split_reviewers("a@x.org, b@x.org") == ["a@x.org", "b@x.org"]
    assert Settings._split_reviewers('["a@x.org"]') == ["a@x.org"]
    assert Settings._split_reviewers(["kept"]) == ["kept"]
    log("OUTPUT", "comma list and JSON array both parse")
