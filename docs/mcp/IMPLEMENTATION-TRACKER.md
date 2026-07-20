# MCP Platform — Implementation Tracker

_The single build plan for `ARCHITECTURE.md` (v0.2). Working doc, roles-only (commit at your discretion)._
_Companion: `BUILD-LOG.md` (what each landed step actually built + test commands)._
_Updated as we build — check boxes, add discoveries, never let it drift from code._

> **▶ Current step:** Phase 1 · **`context_get`** (the `context` bone — calendar), then `assemble_pack`.
> **Last landed:** fetch contract hardening (status/note, self-describing declines), 2026-07-20, 138/18.
> **Providers (local `.env`):** embeddings `openai:text-embedding-3-small`; chat `claude:claude-opus-4-8`.
> **Run server:** `cd apps/api && uv run python -m app.mcp.server` · **Test client:** `scripts/mcp_call.py`

## Status legend
`[ ]` todo · `[~]` in progress · `[x]` done · `[-]` deferred (with reason) · ✅ verified green

## Conventions (locked)
- Tool names use **underscores** (`platform_capabilities`); architecture's dotted names are aliases.
- Tools are **thin carriers**; logic lives in the called modules (tool-vs-prompt litmus, §1).
- Every tool obeys the contract rules **available at its phase** (§1 target-state table below).
- New datasets/analytics/widgets/compositions are **registry rows**, never new tools (§2 count discipline).
- Manual testing only: every step ships a `scripts/mcp_call.py` command in BUILD-LOG.
- Every milestone ships **two Claude queries: a build-time one** ("build me a tool that…", Claude
  calls the tools to scaffold — the priority) **and a run-time one** (use the tool to answer now).

---

## The 16-tool target (ARCHITECTURE §2) — build status

| # | Tool | Bone | Phase | Status | Backing code |
|---|------|------|-------|--------|--------------|
| 1 | `platform_capabilities` | discover | 1 | [x] ✅ | `mcp/registry.py` |
| 2 | `resolve_place_time` | resolve | 1(min)/2 | [ ] | risk-graph admin + `calendar._phase` |
| 3 | `corpus_search` | fetch | 1 | [x] ✅ | `mcp/fetch.py` (status/note contract) |
| 4 | `corpus_document` | fetch | 1 | [x] ✅ | `Corpus.documents` + `raw_path` |
| 5 | `feeds_query` | fetch | 2 | [ ] | `food_security/cropmonitor.py` |
| 6 | `compute_run` | compute | 2 | [ ] | new registry; risk weights pattern |
| 7 | `context_get` | context | 1 | [ ] | `food_security/calendar.citation` |
| 8 | `assemble_pack` | assemble | 1 | [~] | `synthesis.gather_evidence` |
| 9 | `verify_groundedness` | verify | 1 | [ ] | `synthesis.check_grounded` |
| 10 | `record_receipt` | record | 1 | [ ] | new (persist + resolve) |
| 11 | `contribute_submit` | contribute | 2 | [ ] | `food_security/routes` ingest |
| 12 | `compose_run` | compositions | 1 | [ ] | `synthesis.synthesize` as a registry row |
| 13 | `ui_design` | UI | 2 | [ ] | new; theme must be authored first |
| 14 | `ui_catalog` | UI | 2 | [ ] | new |
| 15 | `ui_embed` | UI | 2 | [ ] | apps/web widget routes |
| 16 | `ui_component` | UI | 2.5 | [-] | v1.5 copy-in recipes |

---

## Phase 1 — the Pack proves the bones (SHIPPABLE SLICE)

Goal: a hub developer connects in Claude Code and builds the Monday-bulletin tool end to end
against real tools, no LLM of ours in the path. Deliver Food-Security **Pack v0** (gaps declared).

### Tools
- [x] **Step 1 — `platform_capabilities`** ✅ — honest map (real corpus/calendars, gaps declared).
- [~] **Step 2 — `assemble_pack`** — question (+ optional calendar override) → evidence pack with
      `pack_id`, citations (source/date/validation/relevance/archived-copy), declared gaps.
      Wrap `gather_evidence`; add pack persistence keyed by `pack_id` (feeds record + verify).
- [ ] **Step 3 — `verify_groundedness`** — draft + `pack_id` → pass/fail per check + `report_id`;
      correction mode deferred to Phase 2. Wrap `check_grounded`; resolve pack from `pack_id`.
- [ ] **Step 4 — `record_receipt`** — mint (side-effect of assemble/verify) + resolve by id.
      **record-minimal = persisted receipts with durable resolvable links** (contract floor, §1).
- [x] ✅ **`corpus_search`** — passages w/ passports; below-floor = named decline. `mcp/fetch.py`.
- [x] ✅ **`corpus_document`** — inventory + single-doc passport/trace terminus. `mcp/fetch.py`.
- [ ] **Step 7 — `context_get`** — country/crop (+ override) → calendar phases; override cited
      ADJUSTED + target-pinned; mismatch dropped + declared. Wrap `calendar.citation`.
- [ ] **Step 8 — `resolve_place_time` (minimal)** — country/region + "this season" → date window
      via the calendar; full gazetteer is Phase 2.
- [ ] **Step 9 — `compose_run`** — invoke a pack composition by id (`foodsecurity.brief` as a
      registry row) → governed brief via `synthesize`; for no-LLM/REST consumers.

### Pack v0 + demo
- [ ] Pack v0 manifest: registered sources (GEOGLAM, corpus) + calendars + composition + demo
      receipts + minimal skill; analytics/widgets/climate = declared gaps in the manifest.
- [ ] Skill resource: "compose a food-security brief" (the canonical loop, the build rules).
- [ ] Claude Code **build-demo** dry-run (the §4 walkthrough, beats 1–12).
- [ ] **Toil numbers** measured on the demo ("gate replaces the manual fact-check pass"; "packs
      cut sourcing from days to minutes") — they become the pitch.

### Contract enforcement (Phase 1 floor: rules 1–5 + record-minimal)
- [ ] Rule 1 evidence-attached — every pack/search payload carries passports. (assemble/search)
- [~] Rule 2 declines-say-why — fetch tools ✅ (status/note, advertised in descriptions); assemble/compose pending.
- [ ] Rule 3 replayable — receipts persist what was asked/fetched/verified (record-minimal).
- [ ] Rule 4 human-input-declared — calendar override ADJUSTED + target-pinned (context_get).
- [ ] Rule 5 verdicts server-bound — verify returns server verdict; no client verdict input.

---

## Phase 2 — build-out

- [ ] `resolve_place_time` full — gazetteer/admin geometries + event windows.
- [ ] `feeds_query` — live GEOGLAM (+ CHIRPS/CHIRTS/ERA5 as hub data lists land) with staleness.
- [ ] `compute_run` + **analytics registry** — first hub analytic = the S2S pipeline
      (hand-reviewed, the generification pipeline's design input); PoC threshold formulas.
- [ ] Pack v1: climate feeds + threshold analytics + S2S + widgets + full skills.
- [ ] **Acreage component** — crop-type/land-cover mask source registered (authority-labeled) or
      global fallback (flagged); lifts the declared deferral.
- [ ] `verify_groundedness` **correction mode** — minimally re-grounded draft + flagged spans.
- [ ] Rule 6 evidence-tiering — receipts state platform-registered vs caller-supplied.
- [ ] **Verifier page** — "paste a receipt link → plain-language verdict + declared gaps" (built
      BEFORE issuance scales, §7).
- [ ] UI tools: `ui_design` (author the theme first: hoist hardcoded hexes → a named theme),
      `ui_catalog`, `ui_embed` (MCP Apps resources + iframe fallback; trust classes fail-closed).
- [ ] `contribute_submit` — provenance-enforced ingest door (documents/datasets/analytics/context).
- [ ] `risk.assess` pack — flood assessment refactored into the bones (folds the known backend
      bug list: first-tool-call-only, try/except gaps, naming, routing redundancy — refactor once).
- [ ] Provider layer trial — LangChain adapter behind the registry; gate on
      Anthropic+Gemini+OpenAI+Ollama.
- [ ] Transport: remote-first (streamable-HTTP) + OAuth 2.1 + identity passthrough + audit logs.

---

## Phase 3 — network goods (deliberately last)

- [ ] Contribution opens broadly at dev sprints; **generification pipeline** formalizes
      (activates bone-3 contract requirement); branded contributor credit on packs/receipts.
- [ ] `ui_component` (v1.5) — copy-in recipes (plain HTML + `--grp-*` CSS eject packs, versioned).
- [ ] Receipt institution: append-only transparency log; open self-hostable resolver;
      Ed25519 signed-receipt wire format (interop, not invented); C2PA-compatible manifests.
- [ ] Rule 7 full — immutable retention SLA; center-blind hub-local receipts (hub-signed digests);
      contributor data-tracker (platform + call-out tiers).
- [ ] Independent validation certification (bone-2 target regime, CEOS-ARD two-tier).
- [ ] **Gatekeeper recruitment** — one donor/secretariat/audit office requires resolvable receipts
      (the only proven demand channel; certification/insurance).
- [ ] Multi-org resolver governance (NIIS pattern) before a 2nd sovereign sponsor depends on it.

---

## Cross-cutting / ops (staged across phases)

- [ ] Economics: verdict free at every tier; evidence access priced by residency; public-good
      exemption criteria; Level-2/composite inference metered; free-tier receipt retention costed.
- [ ] Health metrics w/ **intervention thresholds**: hub apps shipped + in weekly use; per-hub
      data freshness; hub-developer return rate. Monthly; triggers outreach. (Never API-call counts.)
- [ ] Residency three-tier: platform-hosted / external call-out / hub-local (own MCP alongside ours).
- [ ] Publish to official MCP registries (signatures + SBOM); never build our own marketplace.
- [ ] Domain Packs registry generic across food-security / global-risk / (future) land-cover.

## Deliberate non-goals (ARCHITECTURE §9) — do NOT build
Beneficiary front ends/portals · shipped LLMs · per-verdict or per-protocol charges · our own
marketplace · land-cover generation · heterogeneous open-ended analytics in core (hub territory) ·
verdict-as-prop UI · logo packs · suppressible declines/gaps/ADJUSTED.

---

## Open dependencies (blockers to name, not silently wait on)
- Climate feeds (F7): hub data lists (CHIRPS/CHIRTS/ERA5/GAEZ/ALOS + S2S) — due ~next hub call.
- Acreage: hub crop-type/land-cover map (authority-labeled) — land-cover hub.
- Gatekeeper: a relying party to require receipts — program lead / donor conversation.
- 2nd recent transcript (platform-overview call) — awaited from user.

## Change log
- **2026-07-20** — `platform_capabilities` ✅; `corpus_search`+`corpus_document` ✅; fetch contract hardened (status/note) ✅ (138/18).
