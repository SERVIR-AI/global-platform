# Precedent research — does a platform like ours exist, and is the LCD shape a failure mode?

**Date:** 2026-07-19 · six-lens web research (geospatial platforms, federated gov/health, trust
institutions, AI-verification market, platform graveyard, MCP-as-product) · ~145 sources.
Full agent output with all URLs: session task record; key findings and lessons digested here.

---

## Q1 — Does this platform exist anywhere?

**Every bone exists and is validated somewhere; the combination exists nowhere; our exact domain
is whitespace.**

| Our piece | Closest living precedent | State |
|---|---|---|
| Curated data + deterministic compute, third parties build real products | **Google Earth Engine** (Global Forest Watch, MapBiomas, SEPAL built on it) | Thriving — validates moats 1+2 |
| BYO-data cataloging under a metadata/provenance schema | **Planetary Computer Pro** (pivoted to exactly this after its free commons died) | Relaunched 6/2026 — validates the contribute/passport bone |
| Generic core + funded regional hubs + domain packs + config-not-code | **DHIS2 + HISP network** (75–80 countries) | Thriving — the organizational twin of the SERVIR-hub model |
| Receipts institution over federated data that stays with owners | **X-Road** (Estonia/Finland/Iceland) | Thriving — strongest validation of receipts/verify as the reason a thin core is trusted |
| Crisis-anchored curated data commons w/ white-glove contributor service | **HDX** | Thriving survivor in humanitarian data |
| Domain knowledge + guardrails + widgets over MCP | **OpenBB** (finance) | Closest full analog; nobody has it in EO/food security |
| Verified-analysis product in our exact domain | **FEWS NET** | 40 years of trust in analysis; wounded by single-funder dependence |
| Food-security decision-support MCP server | **nobody** (only Leaf precision-ag exists in all of agriculture) | Genuine whitespace as of 2026-07 |

## Q2 — Is the lowest-common-denominator shape stretched too thin?

**The worry is validated for the FRAMING and refuted for the ARCHITECTURE.** The graveyard —
GEOSS ("portal of portals," 20 years, declining usage), open-data portals (compliance artifacts,
near-zero use), ChatGPT plugins (raw tool surface, no PMF), Fusion Tables, ONDC retail — all died
leading with generality. The survivors all did three things:

1. **Vertical anchor with an accountable job** (GEE wins = forest MRV/EUDR; SEPAL = REDD+
   reporting; HDX = active crises). → Our El Niño/food-security pack IS the product; the bones
   are its exhaust.
2. **Funded intermediaries doing last-mile fitting** (HISP apprenticeships; HDX field offices;
   DE Africa training). → APIs alone have never converted hubs anywhere; hub enablement is a
   budget line, not a hope.
3. **An authority that gives outputs institutional standing** (Chrome for Certificate
   Transparency; the Digital Signature Act for X-Road; insurers for Truepic). → A receipt nobody
   requires is a nice-to-have; recruit one relying party.

ONDC is the precise empirical test: generic rails collapsed where semantics were heterogeneous
(retail) and thrived where uniform (mobility). Our uniform-semantics primitives (verify, receipt,
passport) are the defensible kind; "any analytic, any domain" is the thin kind — hub territory.

## Hard correctives to our current thinking

- **The verify gate is commoditized.** AWS ($0.10–0.17/1K chars), Google (~$0.00075/1K), Azure
  ship groundedness checking; every detection pure-play pivoted or was absorbed (Patronus,
  Cleanlab, Galileo). Verdicts price at ~zero. The moat is **what the gate checks against**
  (curated, passported, maintained evidence) and **what it feeds** (receipts as audit evidence).
  Sell the loop (pack → verify → receipt), never the check.
- **Receipts have zero proven buyers so far** (EQTY, KYA-OS too early). The only place
  third-party verification commands money today: **certification + insurance** (AIUC-1, Armilla,
  Munich Re aiSure). Route the institution through a relying party whose money moves on the
  evidence (anticipatory-action / parametric-finance donors; auditors of ministry briefs).
- **Receipt-format discipline** (from C2PA/Sigstore/ClaimReview failures): durable short
  links that survive copy-paste (not strippable metadata); free automatic minting as a side
  effect; open self-hostable resolver (no single aggregator — including us — as point of
  failure); append-only public hash log; **brutal claim scoping** ("verified = traceable to pack
  X at time T; NOT verified = truth of pack sources") or the receipt becomes camouflage.
- **Monetization pattern is settled in every precedent:** protocol surface free; charge
  entitlements/subscriptions/hosted operations (FactSet, ODK Cloud, DHIS2 Shared Services Fee);
  decide who-pays before scale (UPI zero-MDR crisis); public-good tier exempt.
- **Config, never code** (DHIS2 vs OpenMRS, same domain/funders — configuration won decisively):
  every hub adaptation must flow through registration APIs (formulas, calendars, sources), never
  core patches.
- **MCP hygiene is survival** (22,000-server sprawl): terse schemas (low-thousands tokens
  total), dynamic discovery, remote-first OAuth 2.1 + identity passthrough + structured audit
  logs (gateway-compatibility = enterprise procurement prerequisite), publish to official
  registries, never build our own marketplace.
- **Widgets have an official standard now:** MCP Apps extension (Anthropic+OpenAI+Microsoft,
  Jan 2026) — ship brief/calendar/provenance-graph as MCP Apps UI resources rendering natively
  in Claude/ChatGPT/Copilot.
- **Single-player test sequencing:** every bone must pay off for ONE hub with ZERO network
  (verify, calendar, evidence packs pass). Contribution registry and receipt institution are
  network goods — sequence last, never lead sales with them.

## Steal list (mechanism → source)

1. **Ship packs, not tools** — the discovery surface is the versioned, importable Food-Security
   Pack (tools+skills+widgets+corpus pre-wired), not 13 raw tools (ChatGPT-plugins lesson; WHO
   metadata-package pattern).
2. **Find our "Chrome"** — one donor/secretariat/audit office that requires resolvable receipt
   links on funded analysis (Certificate Transparency lesson).
3. **A toil number per bone** — measure in the El Niño build: "verify gate replaces the manual
   fact-check pass," "evidence pack cuts sourcing from days to minutes" (DE Africa's "80% less
   prep" pitch). Market numbers, never architecture diagrams.
4. **Branded contributor credit** — every pack/brief/receipt visibly credits "data: Hub X,
   validation L2"; free widget rendering of contributed data (HDX services-for-data).
5. **Correction mode on the gate** — return a minimally-edited re-grounded draft with flagged
   spans, not just pass/fail (Azure correction; Vectara "guardian agent"): converts the gate
   from blocker to the feature hub devs want.
6. **Center-blind receipts + data tracker** — receipts resolve without the platform holding hub
   data; contributors see who queried their data, when, why (X-Road/e-Estonia).
7. **Generification pipeline** — a stated review gate promoting hub-contributed
   formulas/calendars from local → shared-core (DHIS2 App Hub).
8. **Adopt the emerging signed-receipt wire format** (Ed25519 over canonical hash, offline
   verifiable) and differentiate on content-level groundedness attestation (KYA-OS/Checkpoint).
9. **Continuity as a shipped feature** — open-source the receipt resolver; multi-org governance
   so receipts outlive any operator, including us (Killed-by-Google burn).
10. **Instrument disconnection** — per-hub data freshness + hub-developer return rate as monthly
    health metrics with intervention thresholds (GEOSS died slowly and invisibly).
11. **Immutable retention SLA** for anything a receipt references (GEE's removed-datasets
    reproducibility gap is the concrete complaint our receipts answer).
12. **Position above hyperscaler EO platforms** — treat GEE/Planetary Computer/ArcGIS as
    fetchable upstream sources wrapped in our passports; sell the judgment layer they
    structurally lack.

## Bottom line

The architecture survives the research; the pitch inverts. Sell **"FEWS-NET-grade verified
briefs your hub can produce"** — the bones are how. Generality is earned hub by hub, never
claimed up front. The receipts institution is real whitespace with a real adjacent market
(insurance/certification), but it needs a recruited gatekeeper, claim-scope discipline, and
multi-party governance to be worth anything. Nothing found argues against building; several
things found argue against building it the way v0.1 currently *frames* it.
