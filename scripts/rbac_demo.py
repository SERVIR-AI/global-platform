#!/usr/bin/env python
"""The authorization demo, end to end against a running isolated server (started with
`portolan_demo.py serve --fga-url http://127.0.0.1:8090 --review ...`) and a running
OpenFGA. Four people, identified through the platform's own dev-identity header (OAuth
off on a dev box), every decision made by the real store, every call a real MCP call:

  1. the contributor uploads a real raster with visibility 'hub'
  2. while staged, the other hub and the contributor's own hub do not see it
  3. the contributor, also a reviewer, cannot approve their own upload; the reviewer can
  4. the owner's hub sees it and can build a receipt; the other hub and anonymous cannot,
     at the tool and at the data URL alike
  5. the receipt records the store's decision per cited dataset
  6. the owner grants the other hub, which now sees it; then revokes, and it is gone

Run with the project venv:
  .venv/bin/python scripts/rbac_demo.py --url http://127.0.0.1:8027/mcp --fga-url http://127.0.0.1:8090 \
      --state <the serve step's --state dir> --file cache/tiffs/hazard_flood_rp100.tif
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "apps" / "api" / "src"))
sys.path.insert(0, str(REPO / "scripts"))
from portolan_demo import _mcp, _say, _unwrap, _draft  # noqa: E402

DEV_HEADER = "x-grp-dev-identity"
PEOPLE = {"contributor": "demo_contrib", "analyst_mekong": "demo_a1", "analyst_lower": "demo_a2",
          "reviewer": "demo_reviewer"}


def _hdr(person: str) -> dict:
    return {DEV_HEADER: PEOPLE[person]}


def _call(url, person, name, args):
    out = asyncio.run(_mcp(url, [(name, args)], headers=_hdr(person) if person else None))
    return _unwrap(out[name][0]["result"]), out[name][0]["seconds"]


def _serve_file(path: Path, port: int):
    """A plain local http server for the raster the contributor 'links to'."""
    d = path.parent

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(d), **k)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8027/mcp")
    p.add_argument("--fga-url", default="http://127.0.0.1:8090")
    p.add_argument("--state", required=True, help="the serve step's state dir (its cache holds the store ids)")
    p.add_argument("--file", default=str(REPO / "cache" / "tiffs" / "hazard_flood_rp100.tif"))
    p.add_argument("--layer", default="hazard_jrcflood_rp100")
    p.add_argument("--hazard", default="jrcflood", help="the hazard word that resolves to the contributed layer")
    p.add_argument("--place", default="Battambang, Cambodia")
    p.add_argument("--file-port", type=int, default=8899)
    a = p.parse_args(argv)

    # Operator setup, through the platform's own authz module against the same store the
    # server uses (same cache dir, so the same store and model ids).
    os.environ["GRP_FGA_URL"] = a.fga_url
    os.environ["CACHE_DIR"] = str(Path(a.state).resolve() / "cache")
    from app import authz
    ids = authz.bootstrap()
    authz.write([(f"user:{PEOPLE['reviewer']}", "reviewer", authz.PLATFORM),
                 (f"user:{PEOPLE['contributor']}", "reviewer", authz.PLATFORM),   # owner AND reviewer: step 3
                 (f"user:{PEOPLE['contributor']}", "member", "hub:mekong"),
                 (f"user:{PEOPLE['analyst_mekong']}", "member", "hub:mekong"),
                 (f"user:{PEOPLE['analyst_lower']}", "member", "hub:lower")])
    _say("0 operator setup (OpenFGA)", store=ids, roles="reviewer: demo_reviewer, demo_contrib",
         hubs={"mekong": ["demo_contrib", "demo_a1"], "lower": ["demo_a2"]})

    src = Path(a.file).resolve()
    srv = _serve_file(src, a.file_port)
    file_url = f"http://127.0.0.1:{a.file_port}/{src.name}"
    manifest = {"layer": a.layer, "url": file_url,
                "title": "100-year flood hazard, JRC (RBAC demo copy)",
                "description": "Modelled river flood depth for the 100-year return period, "
                               "reclassified to the platform's 1-5 severity scale; a demo copy of "
                               "the JRC global flood hazard map clipped to Southeast Asia.",
                "source": "European Commission Joint Research Centre, global flood hazard maps, "
                          "floodMapGL_rp100y, reclassified by depth on the platform",
                "license": "CC-BY-4.0", "vintage": "2016-11",
                "legend": {1: "Very Low (0-0.5 m)", 2: "Low (0.5-1 m)", 3: "Moderate (1-1.5 m)",
                           4: "High (1.5-2 m)", 5: "Very High (>2 m)"},
                "declared": {"dtype": "int8", "valid_min": 0, "valid_max": 5},
                "visibility": "hub"}
    up, s = _call(a.url, "contributor", "contribute_submit", {"kind": "raster", "manifest": manifest})
    cid = up.get("contribution_id")
    _say("1 the contributor uploads with visibility 'hub'", status=up.get("status"), contribution_id=cid,
         sharing=up.get("sharing"), seconds=s)
    if up.get("status") == "preparing":
        for _ in range(30):
            st, _ = _call(a.url, "contributor", "contribute_status", {"contribution_id": cid})
            if st.get("status") in ("staged", "approved", "declined", "failed"):
                up = st
                break
            time.sleep(2)
        _say("1 (prepared)", status=up.get("status"), sharing=up.get("sharing"))
    if not cid or up.get("status") not in ("staged", "pending"):
        _say("stop", reply=json.dumps(up, default=str)[:1500])
        srv.shutdown()
        return 1

    def listed(person):
        caps, _ = _call(a.url, person, "platform_capabilities", {"section": "raster_layers"})
        names = [l.get("name") for l in (caps.get("raster_layers") or []) if isinstance(l, dict)]
        return a.layer in names
    _say("2 staged: who lists the layer", contributor=listed("contributor"), reviewer=listed("reviewer"),
         analyst_same_hub=listed("analyst_mekong"), analyst_other_hub=listed("analyst_lower"))

    self_try, _ = _call(a.url, "contributor", "contribute_review", {"action": "approve", "contribution_id": cid})
    _say("3a the owner is also a reviewer and tries to approve their own upload", status=self_try.get("status"),
         note=self_try.get("note"))
    ok, s = _call(a.url, "reviewer", "contribute_review", {"action": "approve", "contribution_id": cid})
    pub_dir = Path(a.state).resolve() / "published" / a.layer
    _say("3b the reviewer approves; the publisher writes the Portolan collection on landing",
         status=ok.get("status"), seconds=s,
         published=sorted(x.name for x in pub_dir.iterdir()) if pub_dir.exists() else "not published")

    _say("4 approved: who lists the layer", analyst_same_hub=listed("analyst_mekong"),
         analyst_other_hub=listed("analyst_lower"), reviewer=listed("reviewer"))
    # the hazard word resolves to the contributed layer (hazard_jrcflood_rp100) in the
    # catalog of a caller who may see it, and to nothing for anyone else
    pack, s = _call(a.url, "analyst_mekong", "assemble_pack",
                    {"pack": "risk", "place": a.place, "hazard": a.hazard, "detail": "full"})
    pack_id = pack.get("pack_id")
    used = ((pack.get("stats") or {}).get("hazard") or {})
    _say("4a the same-hub analyst assembles over it", status=pack.get("status"), pack_id=pack_id, seconds=s,
         hazard_layer_used=used.get("layer") if isinstance(used, dict) else used)
    refused, s = _call(a.url, "analyst_lower", "assemble_pack",
                       {"pack": "risk", "place": a.place, "hazard": a.hazard})
    _say("4b the other-hub analyst asks for the same hazard", status=refused.get("status"),
         note=(refused.get("note") or refused.get("declined") or json.dumps(refused, default=str))[:300])
    base = a.url.rsplit("/mcp", 1)[0]
    slug = None
    try:
        import httpx
    except ImportError:
        import httpx2 as httpx
    for clip in sorted((Path(a.state).resolve() / "cache").glob(f"*/{a.layer}.tif")):
        slug = clip.parent.name
        break
    if slug:
        data_url = f"{base}/api/raster/{slug}/{a.layer}.tif"
        codes = {who: httpx.get(data_url, headers=_hdr(who) if who else {}, timeout=60).status_code
                 for who in ("analyst_mekong", "analyst_lower", None)}
        _say("4c the data URL answers the same way", url=data_url, same_hub=codes["analyst_mekong"],
             other_hub=codes["analyst_lower"], anonymous=codes[None])
    else:
        _say("4c the data URL", note="no clip path found in the pack trace; skipped")

    if pack_id:
        question = f"What is the flood risk in {a.place}?"
        draft = _draft(pack, question)
        pub, s = _call(a.url, "analyst_mekong", "publish_answer",
                       {"pack_id": pack_id, "draft": draft, "question": question, "detail": "full"})
        tries = 0
        while pub.get("status") == "blocked" and tries < 3:      # the gate's failures feed the next draft
            tries += 1
            draft = _draft(pack, question, failures=pub.get("failures") or pub)
            pub, s = _call(a.url, "analyst_mekong", "publish_answer",
                           {"pack_id": pack_id, "draft": draft, "question": question, "detail": "full"})
        if pub.get("status") == "blocked":
            _say("5 (the model's draft kept failing the groundedness gate)", failures=pub.get("failures") or pub)
        rid = (pub.get("receipt") or {}).get("id") or pub.get("receipt_id")
        decisions = None
        if rid:
            r = httpx.get(f"{base}/api/resolve/receipt/{rid}", timeout=60)
            decisions = (r.json() or {}).get("access_decisions") if r.status_code == 200 else f"http {r.status_code}"
        _say("5 the receipt records the store's decision", status=pub.get("status"), receipt_id=rid,
             access_decisions=decisions)

    g, _ = _call(a.url, "contributor", "contribute_share", {"contribution_id": cid, "action": "grant", "hub": "lower"})
    _say("6a the owner grants the other hub", status=g.get("status"), sharing=g.get("sharing"),
         other_hub_lists_it=listed("analyst_lower"))
    r, _ = _call(a.url, "contributor", "contribute_share", {"contribution_id": cid, "action": "revoke", "hub": "lower"})
    _say("6b the owner revokes it", status=r.get("status"), sharing=r.get("sharing"),
         other_hub_lists_it=listed("analyst_lower"))
    stranger, _ = _call(a.url, "analyst_lower", "contribute_share", {"contribution_id": cid, "action": "grant", "hub": "lower"})
    _say("6c the other hub cannot grant itself access", status=stranger.get("status"), note=stranger.get("note"))
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
