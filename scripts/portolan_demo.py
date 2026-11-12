#!/usr/bin/env python
"""The published-catalog demo, end to end and with nothing mocked.

    publish   write approved raster layers as a Portolan catalog (STAC 1.1) to a folder
    check     validate that folder with the Portolan CLI, when one is installed
    push      print or run the `aws s3 sync` that puts the folder on Source Cooperative
    serve     start an ISOLATED platform server that reads the catalog as a third source,
              with one layer removed from its local files so it can only come from the catalog;
              with --fga-url at 127.0.0.1 and nothing listening there, also a local OpenFGA on a
              sqlite datastore in --state (roles, hubs and sharing survive a restart)
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
import shlex
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

def _kept_store_ids(state: Path, fga_url: str) -> str | None:
    """The saved authorization store ids (cache/mcp/authz.json) survive the wipe only when
    they name the persisted local OpenFGA this serve starts again: its sqlite file is in
    the state dir and the saved url is the one asked for. Fresh ids against that store
    would make a new, empty store the server trusts while the roles sit in the old one."""
    saved = state / "cache" / "mcp" / "authz.json"
    if not (fga_url and (state / "openfga.db").exists() and saved.exists()):
        return None
    try:
        text = saved.read_text()
        same = str(json.loads(text).get("url", "")).rstrip("/") == fga_url.rstrip("/")
    except (OSError, ValueError):
        return None
    return text if same else None


def _prepare_state(state: Path, remove: list[str], fga_url: str = "") -> dict:
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
    kept = _kept_store_ids(state, fga_url)
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
    if kept:
        (cache / "mcp" / "authz.json").write_text(kept)   # the persisted store's ids
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
            "published": str(state / "published"), "store_ids": "kept" if kept else "fresh"}


def _openfga(state: Path, fga_url: str) -> dict:
    """The authorization store for a local demo: when --fga-url names this machine and
    nothing answers there, start OpenFGA on the sqlite engine in the state dir, so the
    roles, hubs and sharing written today are still there after a restart (the memory
    engine forgets the store; the saved ids then name nothing and every check fails
    closed). A store elsewhere, or one already listening, is left alone."""
    from urllib.parse import urlsplit
    u = urlsplit(fga_url)
    host, port = u.hostname or "", u.port or 80
    if host not in ("127.0.0.1", "localhost"):
        return {"openfga": f"at {fga_url}, run elsewhere"}
    if _listening(port):
        return {"openfga": f"already listening at {fga_url}"}
    exe = shutil.which("openfga")
    if not exe:
        return {"error": f"nothing answers at {fga_url} and the openfga binary is not installed "
                         "(brew install openfga); without its store the server would refuse every "
                         "authorization check, so it is not started"}
    db, log_path = state / "openfga.db", state / "openfga.log"
    engine = ["--datastore-engine", "sqlite", "--datastore-uri", f"file:{db}"]
    # The schema has to exist before `run`; on a migrated database this is a no-op.
    mig = subprocess.run([exe, "migrate", *engine], capture_output=True, text=True)
    if mig.returncode != 0:
        return {"error": f"openfga migrate failed ({mig.returncode}): {(mig.stderr or mig.stdout)[-800:]}"}
    cmd = [exe, "run", *engine, "--http-addr", f"{host}:{port}", "--grpc-addr", f"{host}:{port + 1}",
           "--playground-enabled=false", "--metrics-enabled=false"]
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    (state / "openfga.pid").write_text(str(proc.pid))     # known before the wait, so a failed start can be stopped
    if not _answers(f"{fga_url.rstrip('/')}/healthz", proc, seconds=20):
        proc.terminate()
        return {"error": f"OpenFGA did not come up (exit {proc.poll()}); see {log_path}"}
    return {"openfga": f"started at {fga_url}, sqlite datastore {db}", "pid": proc.pid, "log": str(log_path)}


PERSONAS = {"contributor": ("demo_contrib", "a hub analyst who uploads a layer (hub mekong; also a reviewer, "
                                            "so the self-approval refusal can be shown)"),
            "reviewer": ("demo_reviewer", "the platform reviewer"),
            "analyst_mekong": ("demo_a1", "an analyst in the contributor's hub (mekong)"),
            "analyst_lower": ("demo_a2", "an analyst in another hub (lower)")}
SERVER = "servir"                    # the mcpServers key: Claude Code names its tools mcp__<key>__<tool>


def _fence(ident: str, withheld: list[str]) -> dict:
    """Claude Code settings for one person: a PreToolUse hook on each tool the server does not
    offer them, so a session talked into trying reads the reason in the client instead of
    making the round trip. The server refuses the same call on its own (the store decides),
    so this is the second fence. Which tools are withheld is read from the running server
    (tools/list as the operator minus tools/list as the person), never typed here."""
    hooks = []
    for tool in withheld:
        reason = f"{ident} is not offered {tool} on this platform: the server refuses it as well"
        hooks.append({"matcher": f"mcp__{SERVER}__{tool}",
                      "hooks": [{"type": "command", "command": f"echo {shlex.quote(reason)} >&2; exit 2"}]})
    return {"hooks": {"PreToolUse": hooks} if hooks else {}}


def _fences(state: Path, port: int) -> dict[str, list[str]]:
    """Written once the server answers: for every persona, the tools the server lists for the
    operator (no header) but not for them. Returns {persona: withheld tools}."""
    url = f"http://127.0.0.1:{port}/mcp"
    offered_all = set(asyncio.run(_mcp(url, []))["tools"])
    pdir = state / "personas"
    out = {}
    for name, (ident, _) in PERSONAS.items():
        offered = set(asyncio.run(_mcp(url, [], headers={"x-grp-dev-identity": ident}))["tools"])
        out[name] = sorted(offered_all - offered)
        (pdir / f"{name}.settings.json").write_text(json.dumps(_fence(ident, out[name]), indent=2))
    return out


def _personas(state: Path, cache: str, fga_url: str, port: int) -> None:
    """The operator's one-time setup for the user-journey demo: roles and hub membership
    as tuples in the store the server will use, and one Claude Code MCP config per
    person, each carrying that person's dev identity, beside a settings file with that
    person's fence. Written BEFORE the server starts, so both sides read the same store
    and model ids; every write is idempotent, so a restart on the persisted store is
    harmless."""
    os.environ["GRP_FGA_URL"] = fga_url
    os.environ["CACHE_DIR"] = cache
    from app import authz
    ids = authz.bootstrap()
    authz.write([(f"user:{PERSONAS['reviewer'][0]}", "reviewer", authz.PLATFORM),
                 (f"user:{PERSONAS['contributor'][0]}", "reviewer", authz.PLATFORM),
                 (f"user:{PERSONAS['contributor'][0]}", "member", "hub:mekong"),
                 (f"user:{PERSONAS['analyst_mekong'][0]}", "member", "hub:mekong"),
                 (f"user:{PERSONAS['analyst_lower'][0]}", "member", "hub:lower"),
                 ("user:local-dev", "operator", authz.PLATFORM)])
    # Who is a reviewer is the store's answer to the tuples just written (an operator tuple
    # implies it too), not a second list kept beside them.
    reviewers = {ident for ident, _ in PERSONAS.values()
                 if authz.check(f"user:{ident}", "reviewer", authz.PLATFORM)}
    pdir = state / "personas"
    pdir.mkdir(exist_ok=True)
    lines = []
    for name, (ident, what) in PERSONAS.items():
        cfg = {"mcpServers": {SERVER: {"type": "http", "url": f"http://127.0.0.1:{port}/mcp",
                                       "headers": {"x-grp-dev-identity": ident}}}}
        (pdir / f"{name}.json").write_text(json.dumps(cfg, indent=2))
        (pdir / f"{name}.settings.json").write_text(json.dumps({"hooks": {}}, indent=2))   # fences once the server answers
        lines.append(f"{name:15s} {ident:14s} {what}\n"
                     f"{'':15s} claude --mcp-config {pdir / (name + '.json')} "
                     f"--settings {pdir / (name + '.settings.json')} --strict-mcp-config")
    _say("personas (operator setup)", store=ids,
         hubs={"mekong": ["demo_contrib", "demo_a1"], "lower": ["demo_a2"]},
         reviewers=sorted(reviewers), operator="local-dev (no header)")
    print("\n   Open one Claude Code session per person, from an EMPTY directory:\n")
    for line in lines:
        print("   " + line.replace("\n", "\n   "))
    print()


DEMO_CATALOG = "https://data.source.coop/737847/grp-scratch/portolan/catalog.json"


def _listening(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _answers(url: str, proc, seconds: int) -> bool:
    """True once the url answers 200; False if the process dies first (port taken, import error)."""
    import time
    import urllib.request
    for _ in range(seconds * 2):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return proc.poll() is None


def cmd_serve(a) -> int:
    if a.demo:                                      # the team demo, as one word
        a.catalog = a.catalog or DEMO_CATALOG
        a.remove_layer = a.remove_layer or ["hazard_flood_rp100"]
        a.fga_url = a.fga_url or "http://127.0.0.1:8090"
        a.review = True
        a.background = True
    if not a.catalog:
        print("serve needs --catalog <catalog.json URL or path>, or --demo")
        return 2
    state = Path(a.state).resolve()
    if _listening(a.port):
        # Preparing would wipe the db and re-create the store UNDER the server that is still
        # running the old code; it would then fail every authorization check closed.
        pidfile = state / "server.pid"
        print(f"   a server already listens on 127.0.0.1:{a.port}; stop it first, then rerun:\n"
              f"      kill $(cat {pidfile})" if pidfile.exists() else
              f"   a server already listens on 127.0.0.1:{a.port}; stop it first, then rerun.")
        return 3
    prepared = _prepare_state(state, a.remove_layer, a.fga_url)
    store = _openfga(state, a.fga_url) if a.fga_url else {}
    if store.get("error"):
        print(f"   {store['error']}")
        return 5
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
         authorization=(f"OpenFGA at {a.fga_url} ({store.get('openfga')}; store ids {prepared['store_ids']})"
                        if a.fga_url else "off"),
         review="human review (auto-approve off)" if a.review else "auto-approve on")
    if a.fga_url and a.personas:
        _personas(state, prepared["cache"], a.fga_url, a.port)
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(SRC),
           "--host", "127.0.0.1", "--port", str(a.port)]
    if a.background:
        log = open(state / "server.log", "ab")
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO))
        (state / "server.pid").write_text(str(proc.pid))     # known before the wait, so a failed start can be stopped
        if not _answers(f"http://127.0.0.1:{a.port}/api/health", proc, seconds=90):
            proc.terminate()
            print(f"   the server did not come up (exit {proc.poll()}); see {state / 'server.log'}")
            return 4
        fences = _fences(state, a.port) if a.fga_url and a.personas else {}
        _say("serve", pid=proc.pid, log=str(state / "server.log"),
             stop_server=f"kill $(cat {state / 'server.pid'})",
             **({"stop_openfga": f"kill $(cat {state / 'openfga.pid'})"}
                if (state / "openfga.pid").exists() else {}),
             **({"second_fence": {name: tools for name, tools in fences.items() if tools}} if fences else {}))
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
    s = sub.add_parser("serve"); s.add_argument("--catalog", default=""); s.add_argument("--port", type=int, default=8027)
    s.add_argument("--state", default="/tmp/servir-demo"); s.add_argument("--remove-layer", action="append", default=[])
    s.add_argument("--demo", action="store_true", help="the team demo preset: Source catalog, flood layer removed "
                                                       "locally, OpenFGA on 8090 (started here on sqlite when "
                                                       "nothing listens), review on, background")
    s.add_argument("--read-through", action="store_true"); s.add_argument("--background", action="store_true")
    s.add_argument("--fga-url", default="", help="OpenFGA server (turns the authorization store on); a 127.0.0.1 "
                                                 "url nobody answers is started here, sqlite datastore in --state")
    s.add_argument("--review", action="store_true", help="hold contributions for a reviewer (auto-approve off)")
    s.add_argument("--no-personas", dest="personas", action="store_false",
                   help="skip the demo personas (roles, hubs, one Claude Code config per person)")
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
