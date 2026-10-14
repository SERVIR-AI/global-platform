"""Auto-approve is the default for now (owner's decision, 2026-10-14): every kind lands
on submit and says no human reviewed it; the human review path stays, switched off,
and every surface that describes the gate tells the truth under either setting."""

from app.config import Settings, get_settings
from app.contrib import staging


def test_auto_approve_is_on_by_default_and_review_is_one_switch_away(monkeypatch, log):
    """The field default is on; the suite runs with it off (conftest) to keep testing the
    review path; the submit text describes whichever is in force."""
    assert Settings.model_fields["grp_auto_approve"].default is True
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)
    on = staging.describe_submit()
    log("OUTPUT", on[:160])
    assert "AUTO-APPROVES" in on and "status: approved" in on
    monkeypatch.setattr(get_settings(), "grp_auto_approve", False)
    off = staging.describe_submit()
    assert "STAGED" in off and "until a reviewer decides" in off


def test_the_weights_picker_tells_the_truth_under_either_switch(monkeypatch, log):
    """Auto-approve on: the picker says a valid proposal is live at once and handles the
    'approved' reply; off: it promises review, as before."""
    from app.mcp import weights_ui
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)
    on = weights_ui.html()
    assert "AUTO-APPROVES" in on and 'd.status === "approved"' in on
    assert "auto-approves" in weights_ui.payload()["note"]
    monkeypatch.setattr(get_settings(), "grp_auto_approve", False)
    assert "once a reviewer approves" in weights_ui.html()
