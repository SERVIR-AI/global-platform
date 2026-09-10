"""usage_notes: a few contributor-authored lines the agent reads WHEN USING the
source — "compare with CHIRPS before citing", "screening only, not site
decisions". The skill that travels with the resource. Hard character limit:
guidance is a hint, not a document; a document belongs in the corpus."""

LIMIT = 500


def validate(text) -> list[str]:
    """Failures for one usage_notes value (empty list = valid or absent)."""
    if text is None:
        return []
    if not isinstance(text, str):
        return ["usage_notes must be a string"]
    if len(text) > LIMIT:
        return [f"usage_notes is {len(text)} characters — the limit is {LIMIT}. "
                "Guidance is a hint for the consuming agent, not a document; if it "
                "needs more room, contribute it as a document source instead"]
    return []
