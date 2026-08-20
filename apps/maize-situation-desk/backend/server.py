"""Maize Situation Desk — backend.

Serves the frontend and a small JSON API on ONE port (8080). The browser
talks only to this process. Every /api/ask request runs the platform's full
governed workflow against the GRP MCP server:

    assemble_pack -> draft (via `claude -p`) -> verify_groundedness -> record_receipt

Any stage can decline; declines are returned as data, never hidden.
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import grp_client  # noqa: E402
import tracing  # noqa: E402

PORT = 8080
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
CROP = "maize"
COUNTRIES = {"kenya": "Kenya", "zambia": "Zambia"}

_meta_cache = None


def get_meta():
    """Fetch the platform's own design tokens/components once and cache them.

    Never invent styling: everything the UI needs (logo, colours, component
    markup) comes from the platform's ui_design / ui_component / ui_catalog
    tools, not from hand-authored CSS in this app.
    """
    global _meta_cache
    if _meta_cache is not None:
        return _meta_cache

    caps = grp_client.call_tool("platform_capabilities", {})
    domain_name = caps["domain_packs"][0]["display_name"]

    design = grp_client.call_tool("ui_design", {"format": "all"})
    header = grp_client.call_tool("ui_component", {"name": "platform_header"})
    source_card = grp_client.call_tool("ui_component", {"name": "source_card"})
    groundedness_strip = grp_client.call_tool("ui_component", {"name": "groundedness_strip"})
    decline_card = grp_client.call_tool("ui_component", {"name": "decline_card"})

    header_html = header["markup"].replace("{{domain_name}}", domain_name).replace("{{hub_mark}}", "")

    # The groundedness_strip's built-in script resolves against the platform's
    # own resolver host (localhost:8001). The browser must only ever talk to
    # THIS backend, so repoint its fetch at our /api/resolve proxy.
    gate_html = groundedness_strip["markup"].replace("http://localhost:8001/api/resolve", "/api/resolve")

    _meta_cache = {
        "domain_name": domain_name,
        "css": design["css"],
        "header_html": header_html,
        "source_card_template": source_card["markup"],
        "groundedness_strip_template": gate_html,
        "decline_card_template": decline_card["markup"],
    }
    return _meta_cache


def _build_draft_prompt(question, pack):
    citations_block = "\n".join(
        f"[{c['n']}] {c.get('source', '')} — {c.get('title', c.get('text', ''))[:280]}"
        for c in pack.get("citations", [])
    )
    sections = "\n".join(pack.get("required_sections", []))
    gaps = "\n".join(f"- {g}" for g in pack.get("gaps", [])) or "(none declared)"

    return f"""You are drafting a governed food-security brief for the Global Risk Platform.

QUESTION FROM THE USER:
{question}

COUNTRY: {pack.get("country")}   CROP: {pack.get("crop")}

You must write the brief using EXACTLY these section headers, in this order,
and nothing else as a top-level header (no "## Sources" section — citations
go inline):
{sections}

Cite evidence inline as [n], where n is one of the numbered items below. Every
paragraph must contain at least one [n] citation. Only cite numbers that
appear in this list — never invent a citation number. If the evidence pack
does not cover something, say so plainly in the "What's missing" section
rather than guessing.

EVIDENCE PACK (numbered citations):
{citations_block}

DECLARED GAPS (missing data — mention where relevant, do not hide):
{gaps}

Write the brief now. Output ONLY the brief markdown, starting with the first
required section header. No preamble, no closing remarks."""


def step_resolve_input(country_key, question, steps):
    """Validate the raw request. Not an MCP/LLM call — just records the
    decision so a bad-input decline is traceable like everything else."""
    t0, started = time.perf_counter(), tracing.now_iso()
    country = COUNTRIES.get(country_key)
    inputs = {"country_key": country_key, "question_chars": len(question)}

    def _reject(summary, error):
        steps.append(tracing.make_step(
            name="resolve_input", t_start=t0, t_end=time.perf_counter(),
            started_at=started, ended_at=tracing.now_iso(), summary=summary,
            why="Validates the raw request (country key, question) before anything else runs.",
            inputs=inputs, outcome={"resolved_country": None}, error=error,
        ))

    if not question:
        _reject("Rejected: no question given.", "A question is required.")
        return None
    if not country:
        error = f"Unknown country '{country_key}'. Expected one of {sorted(COUNTRIES)}."
        _reject(f"Rejected country '{country_key}'.", error)
        return None
    steps.append(tracing.make_step(
        name="resolve_input", t_start=t0, t_end=time.perf_counter(),
        started_at=started, ended_at=tracing.now_iso(),
        summary=f"Resolved country key '{country_key}' -> '{country}'.",
        why="Validates the raw request (country key, question) before anything else runs.",
        inputs=inputs, outcome={"resolved_country": country}, error=None,
    ))
    return country


def step_assemble_pack(country, question, steps):
    t0, started = time.perf_counter(), tracing.now_iso()
    inputs = {"country": country, "crop": CROP, "focus": question}
    external = {"service": "grp-mcp", "operation": "assemble_pack",
                "endpoint": grp_client.GRP_MCP_URL, "attempts": 1}
    try:
        pack = grp_client.call_tool("assemble_pack", inputs)
    except Exception as exc:
        steps.append(tracing.make_step(
            name="assemble_pack", t_start=t0, t_end=time.perf_counter(),
            started_at=started, ended_at=tracing.now_iso(),
            summary="assemble_pack call raised an error before returning a pack.",
            why="Gathers the evidence pack (corpus + live feeds + calendar) the brief must be drafted from and verified against.",
            inputs=inputs, outcome=None, error=str(exc),
            external={**external, "outcome_size": None},
        ))
        raise

    declined = pack.get("status") == "declined"
    n_citations = None if declined else len(pack.get("citations", []) or [])
    n_gaps = None if declined else len(pack.get("gaps", []) or [])
    outcome = {
        "pack_status": pack.get("status"),
        "pack_id": pack.get("pack_id"),
        "n_citations": n_citations,
        "n_gaps": n_gaps,
        "declined_note": pack.get("note") if declined else None,
    }
    summary = (
        f"Pack declined: {pack.get('note')}" if declined else
        f"Assembled pack {pack.get('pack_id')} with {n_citations} citation(s) and {n_gaps} declared gap(s)."
    )
    steps.append(tracing.make_step(
        name="assemble_pack", t_start=t0, t_end=time.perf_counter(),
        started_at=started, ended_at=tracing.now_iso(), summary=summary,
        why="Gathers the evidence pack (corpus + live feeds + calendar) the brief must be drafted from and verified against.",
        inputs=inputs, outcome=outcome, error=None,
        external={**external, "outcome_size": n_citations},
    ))
    return pack


def step_draft_brief(question, pack, steps):
    """Draft a brief from an evidence pack using the user's own model
    (`claude -p`, structured JSON output — real token counts and cost,
    not a guess from wall-clock time alone)."""
    t0, started = time.perf_counter(), tracing.now_iso()
    prompt = _build_draft_prompt(question, pack)
    inputs = {
        "question_chars": len(question),
        "pack_id": pack.get("pack_id"),
        "prompt_chars": len(prompt),
        "required_sections": pack.get("required_sections"),
    }

    def _fail(error, llm=None):
        steps.append(tracing.make_step(
            name="draft_brief", t_start=t0, t_end=time.perf_counter(),
            started_at=started, ended_at=tracing.now_iso(),
            summary="Drafting failed — no brief was produced.",
            why="Drafts the brief text from the evidence pack, citing [n], using the user's own model via `claude -p`.",
            inputs=inputs, outcome=None, error=error, llm=llm,
        ))

    try:
        result = subprocess.run(
            [
                "claude", "-p", "--output-format", "json",
                "--disallowedTools",
                "Bash,Read,Write,Edit,WebFetch,WebSearch,Agent,Task",
            ],
            input=prompt, capture_output=True, text=True, timeout=180,
        )
    except Exception as exc:
        _fail(f"subprocess failed to run `claude -p`: {exc}")
        raise RuntimeError(str(exc))

    if result.returncode != 0:
        error = f"claude -p exited {result.returncode}: {result.stderr.strip()[:2000]}"
        _fail(error)
        raise RuntimeError(error)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        error = f"claude -p returned non-JSON output: {exc}"
        _fail(error)
        raise RuntimeError(error)

    draft = (payload.get("result") or "").strip()
    usage = payload.get("usage") or {}
    llm_info = {
        "provider": "anthropic (claude code)",
        "models": sorted((payload.get("modelUsage") or {}).keys()) or None,
        "tokens": {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
            "cache_creation": usage.get("cache_creation_input_tokens"),
        },
        "cost_usd": payload.get("total_cost_usd"),
        "priced_at": "claude -p reported cost (total_cost_usd), computed by the CLI at call time",
        "duration_api_ms": payload.get("duration_api_ms"),
        "num_turns": payload.get("num_turns"),
    }
    outcome = {
        "draft_chars": len(draft),
        "draft_preview": draft[:400],
        "stop_reason": payload.get("stop_reason"),
        "is_error": payload.get("is_error"),
    }
    steps.append(tracing.make_step(
        name="draft_brief", t_start=t0, t_end=time.perf_counter(),
        started_at=started, ended_at=tracing.now_iso(),
        summary=f"Drafted a {len(draft)}-char brief (cost ${payload.get('total_cost_usd', 0):.4f}, {usage.get('output_tokens')} output tokens).",
        why="Drafts the brief text from the evidence pack, citing [n], using the user's own model via `claude -p`.",
        inputs=inputs, outcome=outcome, error=None, llm=llm_info,
    ))
    return draft


def step_verify(draft, pack, steps):
    t0, started = time.perf_counter(), tracing.now_iso()
    pack_id = pack.get("pack_id")
    inputs = {"pack_id": pack_id, "draft_chars": len(draft)}
    external = {"service": "grp-mcp", "operation": "verify_groundedness",
                "endpoint": grp_client.GRP_MCP_URL, "attempts": 1}
    try:
        verify = grp_client.call_tool("verify_groundedness", {"draft": draft, "pack_id": pack_id})
    except Exception as exc:
        steps.append(tracing.make_step(
            name="verify_groundedness", t_start=t0, t_end=time.perf_counter(),
            started_at=started, ended_at=tracing.now_iso(),
            summary="verify_groundedness call raised an error.",
            why="Gates the draft against the evidence pack: required sections present, every paragraph cited, no phantom citations.",
            inputs=inputs, outcome=None, error=str(exc),
            external={**external, "outcome_size": None},
        ))
        raise

    declined = verify.get("status") == "declined"
    outcome = {
        "verify_status": verify.get("status"),
        "passed": None if declined else verify.get("passed"),
        "report_id": None if declined else verify.get("report_id"),
        "evidence_tier": None if declined else verify.get("evidence_tier"),
        "cited": None if declined else verify.get("cited"),
        "phantom_citations": None if declined else verify.get("phantom_citations"),
        "missing_sections": None if declined else verify.get("missing_sections"),
        "uncited_paragraphs": None if declined else verify.get("uncited_paragraphs"),
        "declined_note": verify.get("note") if declined else None,
    }
    if declined:
        summary = f"Verify declined: {verify.get('note')}"
    elif verify.get("passed"):
        summary = f"Groundedness gate PASSED — {len(verify.get('cited') or [])} citation(s) verified, report {verify.get('report_id')}."
    else:
        summary = f"Groundedness gate FAILED — {verify.get('failures') or verify.get('missing_sections') or 'see missing_sections/phantom_citations'}."
    steps.append(tracing.make_step(
        name="verify_groundedness", t_start=t0, t_end=time.perf_counter(),
        started_at=started, ended_at=tracing.now_iso(), summary=summary,
        why="Gates the draft against the evidence pack: required sections present, every paragraph cited, no phantom citations.",
        inputs=inputs, outcome=outcome, error=None,
        external={**external, "outcome_size": len(outcome["cited"] or []) if outcome["cited"] else None},
    ))
    return verify


def step_record_receipt(pack, verify, question, steps):
    t0, started = time.perf_counter(), tracing.now_iso()
    pack_id = pack.get("pack_id")
    report_id = verify.get("report_id")
    inputs = {"pack_id": pack_id, "report_id": report_id, "question_chars": len(question)}
    external = {"service": "grp-mcp", "operation": "record_receipt",
                "endpoint": grp_client.GRP_MCP_URL, "attempts": 1}
    try:
        receipt = grp_client.call_tool(
            "record_receipt", {"pack_id": pack_id, "report_id": report_id, "question": question},
        )
    except Exception as exc:
        steps.append(tracing.make_step(
            name="record_receipt", t_start=t0, t_end=time.perf_counter(),
            started_at=started, ended_at=tracing.now_iso(),
            summary="record_receipt call raised an error.",
            why="Mints the replayable receipt tying the question, pack, and verdict together.",
            inputs=inputs, outcome=None, error=str(exc),
            external={**external, "outcome_size": None},
        ))
        raise

    declined = receipt.get("status") == "declined"
    outcome = {
        "receipt_status": receipt.get("status"),
        "receipt_id": None if declined else receipt.get("receipt_id"),
        "declined_note": receipt.get("note") if declined else None,
    }
    summary = (
        f"Receipt declined: {receipt.get('note')}" if declined else
        f"Minted receipt {receipt.get('receipt_id')}."
    )
    steps.append(tracing.make_step(
        name="record_receipt", t_start=t0, t_end=time.perf_counter(),
        started_at=started, ended_at=tracing.now_iso(), summary=summary,
        why="Mints the replayable receipt tying the question, pack, and verdict together.",
        inputs=inputs, outcome=outcome, error=None,
        external={**external, "outcome_size": 1 if not declined else 0},
    ))
    return receipt


def run_workflow(country_key, question):
    turn_id = tracing.new_turn_id()
    steps = []

    def finish(result):
        """Attach (and best-effort persist) the trace envelope. Tracing must
        never be able to break the response — any failure here is swallowed."""
        try:
            envelope = tracing.assemble_envelope(turn_id, question, country_key, steps, result.get("status"))
            result["trace"] = envelope
            tracing.persist_envelope(envelope)
        except Exception:
            pass
        return result

    try:
        country = step_resolve_input(country_key, question, steps)
        if not country:
            return finish({"status": "declined", "stage": "input", "note": steps[-1]["error"]})

        # 1. Assemble the evidence pack.
        pack = step_assemble_pack(country, question, steps)
        if pack.get("status") == "declined":
            return finish({"status": "declined", "stage": "assemble_pack", "note": pack.get("note", "pack declined"), "pack": pack})

        # 2. Draft the brief with the required sections.
        try:
            draft = step_draft_brief(question, pack, steps)
        except Exception as exc:  # drafting failed -> decline honestly, don't fabricate
            return finish({"status": "declined", "stage": "draft", "note": str(exc), "pack": pack})

        # 3. Verify groundedness.
        verify = step_verify(draft, pack, steps)
        if verify.get("status") == "declined":
            return finish({"status": "declined", "stage": "verify_groundedness", "note": verify.get("note", "verify declined"), "pack": pack, "draft": draft})
        if not verify.get("passed"):
            return finish({
                "status": "declined",
                "stage": "verify_groundedness",
                "note": "The groundedness gate did not pass — the draft is not published.",
                "pack": pack,
                "draft": draft,
                "verify": verify,
            })

        # 4. Mint the receipt.
        receipt = step_record_receipt(pack, verify, question, steps)
        if receipt.get("status") == "declined":
            return finish({"status": "declined", "stage": "record_receipt", "note": receipt.get("note", "receipt declined"), "pack": pack, "draft": draft, "verify": verify})

        return finish({
            "status": "ok",
            "pack": pack,
            "draft": draft,
            "verify": verify,
            "receipt": receipt,
        })
    except Exception as exc:
        # An unexpected bug somewhere above. Whatever steps ran before the
        # crash are still recorded — the trace shows exactly where it stopped.
        return finish({"status": "declined", "stage": "internal", "note": f"Unexpected backend error: {exc}"})


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/meta":
            try:
                self._json(get_meta())
            except Exception as exc:
                self._json({"status": "error", "note": str(exc)}, 500)
            return
        if self.path.startswith("/api/resolve/"):
            # Proxy the resolver so the browser never talks to the platform directly.
            parts = self.path.split("/")  # ['', 'api', 'resolve', 'receipt'|'report'|'pack', id]
            if len(parts) == 5:
                kind, ident = parts[3], parts[4]
                tool_map = {
                    "receipt": ("record_receipt", {"receipt_id": ident}),
                    "report": None,
                    "pack": None,
                }
                if kind == "receipt":
                    try:
                        result = grp_client.call_tool(*tool_map["receipt"])
                        self._json(result)
                    except Exception as exc:
                        self._json({"status": "error", "note": str(exc)}, 500)
                    return
            self._json({"status": "error", "note": "unsupported resolve path"}, 404)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/ask":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            country = (body.get("country") or "").strip().lower()
            question = (body.get("question") or "").strip()
            # run_workflow never raises: every failure path (bad input, MCP
            # error, drafting error, internal bug) is caught internally and
            # returned as a declined result carrying its own trace envelope.
            result = run_workflow(country, question)
            self._json(result)
            return
        self.send_response(404)
        self.end_headers()


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    with ThreadingServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Maize Situation Desk backend serving on http://localhost:{PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
