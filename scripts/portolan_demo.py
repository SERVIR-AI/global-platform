#!/usr/bin/env python
"""The published-catalog demo, end to end and with nothing mocked.

    publish   write approved raster layers as a Portolan catalog (STAC 1.1) to a folder
    check     validate that folder with the Portolan CLI, when one is installed
    push      print or run the `aws s3 sync` that puts the folder on Source Cooperative
    serve     start an ISOLATED platform server that reads the catalog as a third source,
              with one layer removed from its local files so it can only come from the catalog
    run       a real MCP client against that server: capabilities, assemble_pack on the
              catalog-only layer, a real model draft, publish_answer, the receipt over REST
    query     DuckDB over the collection's items.parquet (needs the duckdb package)

Run each step with the project venv: `.venv/bin/python scripts/portolan_demo.py <step> ...`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "apps" / "api" / "src"
sys.path.insert(0, str(SRC))


def _say(step: str, **facts) -> None:
    print(f"\n== {step}")
    for k, v in facts.items():
        text = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
        print(f"   {k}: {text}")


# --- publish / check / push -----------------------------------------------------------

def cmd_publish(a) -> int:
    os.environ["GRP_PORTOLAN_ROOT"] = str(Path(a.root).resolve())
    from app.contrib import portolan
    results = portolan.publish_approved(a.layer or None, a.root)
    for r in results:
        _say(f"publish {r.get('collection', '?')}", **{k: v for k, v in r.items() if k != "collection"})
    bad = [r for r in results if r.get("status") not in ("published", "unchanged", "skipped")]
    return 1 if bad else 0


def cmd_check(a) -> int:
    exe = os.environ.get("PORTOLAN_BIN") or shutil.which("portolan")
    if not exe:
        _say("check", result="portolan CLI not installed (set PORTOLAN_BIN); the tree was not validated")
        return 2
    args = [exe, "check", str(Path(a.root).resolve()), "--json"] + (["--strict"] if a.strict else [])
    out = subprocess.run(args, capture_output=True, text=True)
    print(out.stdout[-6000:] or out.stderr[-3000:])
    _say("check", exit_code=out.returncode)
    return out.returncode


def cmd_push(a) -> int:
    args = ["aws", "s3", "sync", str(Path(a.root).resolve()) + "/", a.dest, "--profile", a.profile,
            "--exclude", ".publishing.*"]
    _say("push", command=" ".join(args), note="writes with the operator's own Source Cooperative login")
    if a.dry_run:
        return 0
    return subprocess.run(args).returncode


# --- serve: an isolated server that can only get one layer from the catalog --------------

def _prepare_state(state: Path, remove: list[str]) -> dict:
    """A separate conf, tiffs dir and cache for the demo server. The real repo cache and
    the contributed YAMLs are never touched; the removed layers exist nowhere locally."""
    import yaml
    state.mkdir(parents=True, exist_ok=True)
    conf = state / "conf"
    conf.mkdir(exist_ok=True)
    rows = yaml.safe_load((REPO / "conf" / "tiffs.contrib.yml").read_text()) or {}
    contracts = yaml.safe_load((REPO / "conf" / "raster_schema.contrib.yml").read_text()) or {}
    for layer in remove:
        rows.pop(layer, None)
        (contracts.get("layers") or {}).pop(layer, None)
    (conf / "tiffs.contrib.yml").write_text(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True))
    (conf / "raster_schema.contrib.yml").write_text(yaml.safe_dump(contracts, sort_keys=False, allow_unicode=True))
    tiffs = state / "tiffs"
    if tiffs.exists():
        shutil.rmtree(tiffs)
    tiffs.mkdir()
    removed_files = {f"{layer}.tif" for layer in remove}
    for f in sorted((REPO / "cache" / "tiffs").glob("*.tif")):
        if f.name not in removed_files and not f.name.startswith("staged-"):
            os.symlink(f, tiffs / f.name)
    cache = state / "cache"
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir()
    for name in ("rag", "cropmonitor"):                   # read-only corpora: linked
        if (REPO / "cache" / name).exists():
            os.symlink(REPO / "cache" / name, cache / name)
    for name in ("vectors", "tables"):                    # small, written by the gate: copied
        if (REPO / "cache" / name).exists():
            shutil.copytree(REPO / "cache" / name, cache / name, symlinks=True)
    (cache / "mcp").mkdir()                               # receipts: a fresh database
    bundles = 0
    for d in sorted((REPO / "cache").glob("*")):          # warmed areas, minus the removed clips
        if d.is_dir() and (d / "meta.json").exists():
            shutil.copytree(d, cache / d.name, symlinks=True)
            for layer in remove:
                for clip in (cache / d.name).glob(f"{layer}*.tif"):
                    clip.unlink()
            bundles += 1
    (state / "published").mkdir(exist_ok=True)
    return {"conf": str(conf), "tiffs": str(tiffs), "cache": str(cache), "bundles": bundles,
            "published": str(state / "published")}


def cmd_serve(a) -> int:
    state = Path(a.state).resolve()
    prepared = _prepare_state(state, a.remove_layer)
    env = {**os.environ,
           "CACHE_DIR": prepared["cache"], "TIFFS_DIR": prepared["tiffs"],
           "TIFFS_CONTRIB_PATH": f"{prepared['conf']}/tiffs.contrib.yml",
           "RASTER_SCHEMA_CONTRIB_PATH": f"{prepared['conf']}/raster_schema.contrib.yml",
           "GRP_DB_PATH": f"{prepared['cache']}/mcp/grp.db",
           "GRP_CATALOG_URL": a.catalog, "GRP_CATALOG_MIRROR": "0" if a.read_through else "1",
           "GRP_PORTOLAN_ROOT": prepared["published"],
           "GRP_OAUTH_ENABLED": "0", "GRP_MCP_TRANSPORT": "http",
           "GRP_CONTRIB_ALLOW_PRIVATE_HOSTS": "1",
           "GRP_AUTO_APPROVE": "0" if a.review else "1",
           **({"GRP_FGA_URL": a.fga_url} if a.fga_url else {})}
    _say("serve", state=str(state), catalog=a.catalog, removed_locally=a.remove_layer,
         mode="read-through (/vsicurl/)" if a.read_through else "mirror on first use (checksum verified)",
         warmed_areas=prepared["bundles"], mcp_url=f"http://127.0.0.1:{a.port}/mcp",
         authorization=(f"OpenFGA at {a.fga_url}" if a.fga_url else "off"),
         review="human review (auto-approve off)" if a.review else "auto-approve on")
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(SRC),
           "--host", "127.0.0.1", "--port", str(a.port)]
    if a.background:
        log = open(state / "server.log", "ab")
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO))
        (state / "server.pid").write_text(str(proc.pid))
        _say("serve", pid=proc.pid, log=str(state / "server.log"))
        return 0
    os.execvpe(cmd[0], cmd, env)


# --- run: a real MCP client ----------------------------------------------------------------

async def _mcp(url: str, calls, headers: dict | None = None):
    from mcp import ClientSession
    from mcp.client import streamable_http as sh
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    client = sh.create_mcp_http_client(headers=headers)
    client.timeout = httpx.Timeout(900.0, connect=30.0)
    async with sh.streamable_http_client(url, http_client=client) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            out = {"tools": sorted(tools)}
            for name, args in calls:
                t0 = time.perf_counter()
                res = await session.call_tool(name, args)
                body = getattr(res, "structuredContent", None)
                if body is None:
                    texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
                    try:
                        body = json.loads(texts[0]) if texts else {}
                    except ValueError:
                        body = {"text": "\n".join(texts)}
                out[name] = out.get(name, []) + [{"args": args, "seconds": round(time.perf_counter() - t0, 2),
                                                  "result": body}]
            return out


def _unwrap(body):
    return body.get("result", body) if isinstance(body, dict) and set(body) == {"result"} else body


def _draft(pack: dict, question: str, failures=None) -> str:
    """A real model drafts the brief from the pack, citing [n] as the loop requires."""
    from app.config import get_settings
    from app.llm import client as llm
    provider = str(getattr(get_settings(), "default_provider", None) or os.environ.get("DEFAULT_PROVIDER", "claude"))
    model = llm.default_model(provider)
    cl = llm.build_client(provider)
    sections = pack.get("required_sections") or []
    cites = pack.get("citations") or []
    numbered = "\n".join(f"[{c.get('n')}] {json.dumps({k: v for k, v in c.items() if k != 'n'}, default=str)[:1500]}"
                         for c in cites if isinstance(c, dict))
    prompt = (f"Question: {question}\n\nWrite a short decision brief with exactly these sections, "
              f"each as a markdown heading in this order: {', '.join(sections)}. Every factual sentence "
              "cites one of the numbered citations below as [n], using only numbers that exist. Use only "
              "facts and numbers that appear in the evidence. HARD RULE on figures: write a figure only "
              "if you copy it character for character from a citation's text or from STATS (same digits, "
              "same separators, no rounding, no unit conversion, no 'about', no totals you computed); "
              "prefer the platform's own computed figures in STATS over figures quoted in documents; "
              "where the evidence declares a gap, say so in the matching section. No introduction, no closing.\n\n"
              + (f"A previous draft was rejected by the groundedness gate for these reasons; fix every one:\n"
                 f"{json.dumps(failures, default=str)[:4000]}\n\n" if failures else "")
              + "CITATIONS:\n" + numbered + "\n\nSTATS:\n" + json.dumps(pack.get("stats"), default=str)[:20000]
              + "\n\nDECLARED GAPS:\n" + json.dumps(pack.get("gaps"), default=str)[:6000])
    resp = cl.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],
                                      temperature=0)
    return resp.choices[0].message.content or ""


def cmd_run(a) -> int:
    question = a.question or f"What is the {a.hazard} risk in {a.place}?"
    first = asyncio.run(_mcp(a.url, [
        ("platform_capabilities", {"section": "catalog_sources"}),
        ("platform_capabilities", {"section": "raster_layers"}),
        ("assemble_pack", {"pack": a.pack, "place": a.place, "hazard": a.hazard,
                           "layers": [a.layer], "detail": "full"}),
    ]))
    sources = _unwrap(first["platform_capabilities"][0]["result"]).get("catalog_sources")
    _say("1 catalog sources (platform_capabilities)", sources=sources)
    layers = _unwrap(first["platform_capabilities"][1]["result"]).get("raster_layers") or []
    mine = [l for l in layers if isinstance(l, dict) and l.get("name") == a.layer]
    _say("2 the layer exists only in the published catalog", listed_as=mine or "NOT LISTED")
    pack_call = first["assemble_pack"][0]
    pack = _unwrap(pack_call["result"])
    pack_id = pack.get("pack_id") or pack.get("id")
    trace = pack.get("trace") or []
    events = trace if isinstance(trace, list) else (trace.get("io") or trace.get("events") or [])
    io = [e for e in events if isinstance(e, dict) and "published catalog" in json.dumps(e, default=str)]
    cited = [c for c in (pack.get("citations") or []) if a.layer in json.dumps(c)]
    gaps = [g for g in (pack.get("gaps") or []) if "catalog" in str(g)]
    _say("3 assemble_pack over the catalog-only layer (cold)", status=pack.get("status"), pack_id=pack_id,
         seconds=pack_call["seconds"], citations_naming_the_layer=len(cited),
         catalog_io_events=io[:3], catalog_gap_lines=gaps)
    if not pack_id:
        _say("stop", reason="no pack id", reply=json.dumps(pack, default=str)[:1500])
        return 1
    t0 = time.perf_counter()
    draft = _draft(pack, question)
    _say("4 a real model drafted the brief", seconds=round(time.perf_counter() - t0, 1),
         chars=len(draft), head=draft[:300].replace("\n", " "))
    second = asyncio.run(_mcp(a.url, [
        ("publish_answer", {"pack_id": pack_id, "draft": draft, "question": question, "detail": "full"}),
        ("assemble_pack", {"pack": a.pack, "place": a.place, "hazard": a.hazard,
                           "layers": [a.layer], "detail": "concise"}),
    ]))
    pub = _unwrap(second["publish_answer"][0]["result"])
    receipt_id = (pub.get("receipt") or {}).get("id") or pub.get("receipt_id")
    if not receipt_id and pub.get("status") == "blocked":
        # The gate said no: one fix pass with its failures in hand (a real second draft).
        failures = pub.get("failures") or pub.get("problems") or pub.get("reasons") or pub
        _say("5a the gate blocked the first draft", failures=failures)
        draft = _draft(pack, question, failures=failures)
        again = asyncio.run(_mcp(a.url, [("publish_answer", {"pack_id": pack_id, "draft": draft,
                                                                 "question": question, "detail": "full"})]))
        second["publish_answer"] = again["publish_answer"]
        pub = _unwrap(again["publish_answer"][0]["result"])
        receipt_id = (pub.get("receipt") or {}).get("id") or pub.get("receipt_id")
    _say("5 publish_answer gated the draft and minted a receipt", status=pub.get("status"),
         verdict=pub.get("verdict") or pub.get("gate") or {k: v for k, v in pub.items()
                                                          if k in ("accepted", "groundedness", "note", "failures")},
         receipt_id=receipt_id, seconds=second["publish_answer"][0]["seconds"])
    if receipt_id:
        try:
            import httpx
        except ImportError:
            import httpx2 as httpx
        base = a.url.rsplit("/mcp", 1)[0]
        r = httpx.get(f"{base}/api/resolve/receipt/{receipt_id}", timeout=60)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        _say("6 the receipt resolves over REST", url=f"{base}/api/resolve/receipt/{receipt_id}",
             http=r.status_code, passed=body.get("passed") if isinstance(body, dict) else None,
             target=body.get("target") if isinstance(body, dict) else None,
             cites_the_layer=a.layer in r.text, draft_sha256=(body.get("draft_sha256") or "")[:16] + "…"
             if isinstance(body, dict) else "")
    warm = second["assemble_pack"][0]
    _say("7 assemble_pack again (warm)", seconds=warm["seconds"], status=_unwrap(warm["result"]).get("status"))
    if a.mirror_dir:
        import hashlib
        files = sorted(Path(a.mirror_dir).glob("stac__*"))
        _say("8 the mirrored file on disk", files=[{"file": f.name, "bytes": f.stat().st_size,
                                                   "sha256": hashlib.sha256(f.read_bytes()).hexdigest()[:16] + "…"}
                                                  for f in files] or "none (read-through mode)")
    return 0


# --- query: DuckDB over the item mirror -----------------------------------------------------

def cmd_query(a) -> int:
    """DuckDB reads the published catalog directly, no server in between: the root's
    child links name the collections, and a bbox predicate over their extents answers
    'which layers cover this area' (plain HTTPS cannot glob, so the list is explicit)."""
    try:
        import duckdb
    except ImportError:
        _say("query", result="duckdb is not installed in this interpreter; run with the tools venv "
                             "(PORTOLAN_TOOLS_PY) or `uv run --with duckdb`")
        return 2
    import urllib.request
    root = a.root_url.rstrip("/")
    if root.startswith("http"):
        req = urllib.request.Request(root + "/catalog.json", headers={"User-Agent": "servirplatform-demo"})
        cat = json.load(urllib.request.urlopen(req, timeout=30))
    else:
        cat = json.load(open(root + "/catalog.json"))
    colls = [root + "/" + lk["href"].lstrip("./") for lk in cat.get("links", []) if lk.get("rel") == "child"]
    con = duckdb.connect()
    if root.startswith("http"):
        con.execute("INSTALL httpfs; LOAD httpfs;")
    t0 = time.perf_counter()
    urls = ", ".join(f"'{u}'" for u in colls)
    sql = (f"SELECT id, title, version, extent.spatial.bbox[1] AS bbox, license "
           f"FROM read_json_auto([{urls}], union_by_name=true) "
           f"WHERE extent.spatial.bbox[1][1] <= {a.east} AND extent.spatial.bbox[1][3] >= {a.west} "
           f"AND extent.spatial.bbox[1][2] <= {a.north} AND extent.spatial.bbox[1][4] >= {a.south}")
    rows = con.execute(sql).fetchall()
    _say("DuckDB bbox query over the published collections, no server",
         collections=len(colls), sql=sql, rows=rows, seconds=round(time.perf_counter() - t0, 3))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("publish"); s.add_argument("--root", required=True); s.add_argument("--layer", action="append")
    s = sub.add_parser("check"); s.add_argument("--root", required=True); s.add_argument("--strict", action="store_true")
    s = sub.add_parser("push"); s.add_argument("--root", required=True)
    s.add_argument("--dest", default="s3://737847/grp-scratch/portolan/"); s.add_argument("--profile", default="source-coop")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("serve"); s.add_argument("--catalog", required=True); s.add_argument("--port", type=int, default=8027)
    s.add_argument("--state", default=str(REPO / "cache" / ".portolan-demo")); s.add_argument("--remove-layer", action="append", default=[])
    s.add_argument("--read-through", action="store_true"); s.add_argument("--background", action="store_true")
    s.add_argument("--fga-url", default="", help="OpenFGA server (turns the authorization store on)")
    s.add_argument("--review", action="store_true", help="hold contributions for a reviewer (auto-approve off)")
    s = sub.add_parser("run"); s.add_argument("--url", default="http://127.0.0.1:8027/mcp")
    s.add_argument("--layer", default="hazard_flood_rp100"); s.add_argument("--place", default="Battambang, Cambodia")
    s.add_argument("--hazard", default="flood"); s.add_argument("--pack", default="risk"); s.add_argument("--question")
    s.add_argument("--mirror-dir")
    s = sub.add_parser("query"); s.add_argument("--root-url", required=True); s.add_argument("--collection", required=True)
    s.add_argument("--west", type=float, default=102.6); s.add_argument("--south", type=float, default=12.8)
    s.add_argument("--east", type=float, default=103.5); s.add_argument("--north", type=float, default=13.6)
    a = p.parse_args(argv)
    return {"publish": cmd_publish, "check": cmd_check, "push": cmd_push, "serve": cmd_serve,
            "run": cmd_run, "query": cmd_query}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
