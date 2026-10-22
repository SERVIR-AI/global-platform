"""A landed table is parsed once per file version, still refused when its bytes change
out-of-band, and the contributed-address check is not repeated on every read."""
import hashlib

import pytest

from app.mcp import feeds


def _spec(path, sha):
    return {"title": "t", "cadence": "irregular", "vintage": "2026-10",
            "fetch": {"path": str(path), "sha256": sha, "columns": {"site": "site", "v": "v"}, "units": "u"}}


def test_a_table_is_parsed_once_per_version_and_out_of_band_edits_are_refused(tmp_path, monkeypatch):
    p = tmp_path / "t.csv"
    p.write_text("site,v\na,1\nb,2.5\n")
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    monkeypatch.setattr(feeds, "_landed_table", lambda path: p)
    reads = []
    real = type(p).read_bytes
    monkeypatch.setattr(type(p), "read_bytes", lambda self: reads.append(1) or real(self))
    one = feeds._adapt_generic_csv({"limit": 10}, _spec(p, sha))
    two = feeds._adapt_generic_csv({"limit": 10}, _spec(p, sha))
    assert one["records"] == two["records"] == [{"site": "a", "v": 1}, {"site": "b", "v": 2.5}]
    assert len(reads) == 1                                   # parsed once
    p.write_text("site,v\na,9\nb,2.5\nc,3\n")               # edited out of band
    with pytest.raises(feeds.FeedDecline):
        feeds._adapt_generic_csv({"limit": 10}, _spec(p, sha))


def test_the_address_check_is_kept_for_a_few_minutes(monkeypatch):
    from app.contrib import fetch_policy
    calls = []
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: calls.append(url) or [])
    assert feeds._checked_url("https://x.test/a.csv") == []
    assert feeds._checked_url("https://x.test/a.csv") == []
    assert calls == ["https://x.test/a.csv"]
