"""Mangrove sprint round 3, feeds: a JSON feed that declares as_of_field reported the
LAST record in publication order as 'latest'. The real OBIS page mixed offset datetimes,
'Z', bare dates, YYYY-MM, ranges and missing dates; one missing date made the sort give
up, so 'latest 2021-08-22' was served over records reaching 2026-07-08."""
from app.contrib import staging
from app.mcp import feeds

from test_sprint_fix2_gate import OWNER, _as, _feed, auto, hub  # noqa: F401  (fixture reuse)

OCC_URL = "https://api.obis.example/v3/occurrence?geometry=POLYGON((...))&size=300"
OCC_FIELDS = {"scientific_name": "scientificName", "lat": "decimalLatitude",
              "lon": "decimalLongitude", "event_date": "eventDate"}
IN_BOX = (10.1, -84.9)        # inside [-85.0, 10.0, -84.8, 10.2]
OUT_BOX = (9.95, -85.2)
# publication order, as OBIS sent it: newest record mid-page, the page ends on 2021
DATES = [
    ("1995-02-13T12:00:00Z", OUT_BOX), ("1980-04-14T12:00:00Z", IN_BOX),
    (None, OUT_BOX), ("1952-08-06", OUT_BOX), ("2023-03-10T10:28:00-06:00", OUT_BOX),
    ("1965-01", IN_BOX), ("2026-07-08T07:50:32-06:00", OUT_BOX),       # the true latest
    ("2026-07-08T12:00:00Z", OUT_BOX),       # later as text, 1h50 earlier as an instant
    ("2015-01-01/2015-12-31", OUT_BOX), ("2019-05-12T16:23:02-04:00", OUT_BOX),
    ("2024-11-05T08:46:00-06:00", OUT_BOX), (None, IN_BOX), ("not recorded", OUT_BOX),
    ("2026-01-16T15:37:00-06:00", IN_BOX),   # the latest inside the box
    ("2017-10-21T15:21:00-06:00", IN_BOX), ("2023-08-11T09:56:17-06:00", OUT_BOX),
    ("2026-01-20T10:26:02-06:00", OUT_BOX), ("2020-03-01T08:00:00+01:00", OUT_BOX),
    ("1969-01-09T12:00:00Z", IN_BOX),        # the last published inside the box
    ("2021-08-22T10:58:00-06:00", OUT_BOX),  # the last published overall
]
OCC = {"total": 4468, "results": [
    {"scientificName": f"taxon {i}", "decimalLatitude": lat, "decimalLongitude": lon,
     "eventDate": d} for i, (d, (lat, lon)) in enumerate(DATES)]}


def test_latest_is_the_most_recent_date_served_not_the_last_record(hub, auto, log):
    hub["api"][OCC_URL] = OCC
    spec = _feed("test_obis_occ", OCC_URL, OCC_FIELDS)
    spec["fetch"]["as_of_field"] = "event_date"
    assert staging.submit("feed", spec, OWNER)["status"] == "approved"

    allof = _as(OWNER, lambda: feeds.query("test_obis_occ", {"limit": 300}))
    log("OUTPUT", allof["summary"])
    assert allof["as_of"] == allof["passport"]["as_of"] == "2026-07-08T07:50:32-06:00"
    assert "latest event_date 2026-07-08T07:50:32-06:00" in allof["summary"]
    assert "ordered by event_date, oldest first" in allof["summary"]
    # undated records are kept — first, never latest — and every record is served
    assert allof["count"] == len(DATES)
    assert [r["event_date"] for r in allof["records"][:3]] == [None, None, "not recorded"]
    assert "3 record(s) with no readable event_date are kept" in allof["summary"]
    assert [r["event_date"] for r in allof["records"][-2:]] == [
        "2026-07-08T12:00:00Z", "2026-07-08T07:50:32-06:00"]

    # the default page is the most recent twelve by event_date, not the publication tail
    page = _as(OWNER, lambda: feeds.query("test_obis_occ"))
    log("OUTPUT", page["summary"])
    assert page["count"] == 12 and page["as_of"] == "2026-07-08T07:50:32-06:00"
    assert "these 12 are the most recent" in page["summary"]
    served = {r["event_date"] for r in page["records"]}
    assert "2026-01-20T10:26:02-06:00" in served and "1969-01-09T12:00:00Z" not in served
    assert all(r["event_date"] not in (None, "not recorded") for r in page["records"])

    # a bbox: latest among the matches, not the last match published
    box = _as(OWNER, lambda: feeds.query("test_obis_occ", {"bbox": [-85.0, 10.0, -84.8, 10.2]}))
    log("OUTPUT", box["summary"])
    assert box["count"] == 6 and box["as_of"] == "2026-01-16T15:37:00-06:00"
    assert "latest event_date 2026-01-16T15:37:00-06:00" in box["summary"]
    assert box["records"][-1]["event_date"] == "2026-01-16T15:37:00-06:00"


def test_another_pack_epoch_feed_published_newest_first(hub, auto):
    """A risk-pack feed shaped like USGS: epoch milliseconds, newest first, one gap."""
    url = "https://quakes.example/4.5_month.geojson"
    hub["api"][url] = {"results": [{"p": {"mag": m, "time": t}} for m, t in
                                   [(5.1, 1760800000000), (4.6, None), (4.9, 1760700000000),
                                    (6.2, 1758300000000)]]}
    spec = _feed("test_quakes", url, {"magnitude": "p.mag", "time": "p.time"}, pack="risk")
    spec["fetch"]["as_of_field"] = "time"
    assert staging.submit("feed", spec, OWNER)["status"] == "approved"
    out = _as(OWNER, lambda: feeds.query("test_quakes", {"limit": 2}))
    assert out["as_of"] == 1760800000000
    assert [r["magnitude"] for r in out["records"]] == [4.9, 5.1]
    assert "ordered by time, oldest first — these 2 are the most recent" in out["summary"]


def test_a_feed_without_as_of_field_is_unchanged(hub, auto):
    hub["api"][OCC_URL] = OCC
    assert staging.submit("feed", _feed("test_obis_plain", OCC_URL, OCC_FIELDS),
                          OWNER)["status"] == "approved"
    out = _as(OWNER, lambda: feeds.query("test_obis_plain", {"limit": 3}))
    assert out["as_of"] is None
    assert [r["event_date"] for r in out["records"]] == [d for d, _ in DATES[-3:]]
    assert out["summary"].endswith("(no timestamp field declared, so recency is unverified)")


def test_a_declared_field_no_record_can_read_sets_no_latest(hub, auto):
    url = "https://api.example/undated"
    hub["api"][url] = {"results": [{"eventDate": d} for d in (None, "unknown", "")]}
    spec = _feed("test_undated", url, {"event_date": "eventDate"}, pack="food-security")
    spec["fetch"]["as_of_field"] = "event_date"
    assert staging.submit("feed", spec, OWNER)["status"] == "approved"
    out = _as(OWNER, lambda: feeds.query("test_undated"))
    assert out["as_of"] is None and out["count"] == 3
    assert "no record carries a readable event_date, so recency is unverified" in out["summary"]
