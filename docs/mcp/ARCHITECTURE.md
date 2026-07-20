# Global Risk Platform — Architecture v0.2 (from scratch)

**Status:** working architecture, supersedes `PROPOSAL.md` (v0.1, kept as the 2026-07-14 call
record).
**Date:** 2026-07-19
**Inputs:** five call transcripts (receipts in `../food-security/transcript-insights-log.md`),
the hub-fit assessment (`../food-security/call-insights.md`), and the six-lens precedent research
(`precedent-research.md`, ~145 sources).

---

## 0. What this is, in one paragraph

The platform is a **hosted MCP server** (with a twin REST mount over the same functions) exposing
**eight primitive capabilities — the bones — bound by a seven-rule trust contract**, and shipped
to the world as **versioned Domain Packs** (food security first). Hub builders consume it twice
with one surface: at **build time**, their own AI client reads our tools, skills, and widget
catalog to assemble *their* app; at **run time**, the app they built calls the same server. We
operate three core things — the server, the registries, and the receipt institution — **plus two
starter kits** (a reference agent and a demo UI), both explicitly designed to become minority
clients, not operated products. **No LLM sits in the tool path**; the reference agent and the
server-side composite path do run one, and that inference cost is priced in §6. Consumers bring
their own model, data, and judgment; starter kits bridge the capacity gap.

**Positioning (from the precedent research):** we sit *above* the hyperscaler data platforms
(Earth Engine, Planetary Computer, ArcGIS become upstream sources wrapped in our provenance
passports) and sell the layer they structurally lack — **judgment, passported evidence, and
receipts** (not "verification": groundedness checking itself is commoditized, per §2 bone 6 —
the loop is ours, the check is table stakes). The pitch is never the architecture; it is the
product it makes: *"briefs with FEWS-NET-grade sourcing discipline — every claim traceable,
every gap declared — and the bones to make more of them."* (Scoped deliberately: the receipt
attests traceability, not the truth of sources — the pitch must never promise more than the
artifact does.) Generality is earned domain by domain, never claimed up front (the
GEOSS/plugins graveyard rule).

---

## 1. The trust contract (seven rules + a litmus)

The contract *is* the platform; the tools are its carriers. It is the **target state**: each
rule carries an availability phase (mirroring the bones table's status discipline), and Phase 1
ships with rules 1–5 fully enforced plus `record`'s minimum viable form — **persisted receipts
with durable resolvable links** (without which §4's "ships with a receipt link or ships visibly
naked" convention would self-disqualify our own demo). Evidence tiering (rule 6) lands with the
first caller-supplied-evidence path; the retention SLA and open resolver (rule 7) are Phase 2–3
governance deliverables, stated as commitments from day one.

1. **Evidence attached.** Every insight carries sources, dates, validation levels, relevance,
   archived-copy links, staleness flags — in the payload, never added by a UI.
2. **Declines say why.** No corpus / below relevance floor / stale feed / out of scope — a
   refusal is a structured answer, not an error.
3. **Everything replayable.** Every answer has a receipt: what was asked, resolved, fetched
   (with passports), computed (which analytic, which version), verified, declined.
4. **Human input declared.** Adjustments (calendars, thresholds, weights) are honored, labeled
   ADJUSTED, and pinned to their target; a mismatched override is dropped *and the drop is
   declared*.
5. **Verdicts are server-bound.** No UI element we ship can be told to render a positive
   verification state; verdicts resolve from the server or render "unverified." Fail-closed.
6. **Receipts state their evidence tier.** "Verified against platform-registered sources" is a
   different claim from "verified against caller-supplied evidence — sources not vetted by this
   platform," and the receipt says which, loudly. (The anti-laundering rule.)
7. **Receipts outlive everything.** Anything a receipt references — evidence packs, document
   versions, analytic versions — is retained immutable under a stated SLA, and the receipt
   resolver is open-source and self-hostable so the institution survives any operator, including
   us. (The reproducibility gap that burns Earth Engine users; the Killed-by-Google lesson.)

**Residency caveat to rules 3 and 7:** for **hub-local** evidence (the center-blind tier), the
receipt records a **hub-signed evidence digest**, not the evidence; retention and query-audit
obligations transfer to the hub, *and the receipt says so*. The contributor data-tracker applies
to platform-hosted and call-out tiers only. Center-blindness and replayability are reconciled by
honesty, not pretense.

**The tool-vs-prompt litmus:** a capability ships as a *tool* only if it has at least one of
**execution** (computes deterministically rather than asking a model), **state** (mints or reads
durable records), or **separation** (runs where the caller can't tamper). Anything with none of
the three is a prompt wearing a costume — we ship it honestly as a **skill**.

---

## 2. The eight bones

The lowest common denominator across every use case in five transcripts is not data or domains —
it is these verbs. Domains are configurations of them. Each bone must pass the **single-player
test** (valuable to one hub with zero network) or be explicitly sequenced as a network good; each
carries a **toil number** we measure in the flagship build (the Digital-Earth-Africa rule: sell
"removes 80% of prep," never an architecture diagram).

| # | Bone | What it does | Contract behavior | Exists today |
|---|---|---|---|---|
| 1 | **resolve** | Human place-and-time → machine place-and-time: names → admin geometries; "this season" → date windows *via the locally correct calendar*; events → windows. | Wraps commodity geocoding upstreams rather than rebuilding them; adds the season/calendar/event semantics they lack. | ◐ (risk-graph admin resolution + calendar phases; not yet a first-class tool) |
| 2 | **fetch** | Data with its **passport**: source, date, **validation level** (target: earned certification, CEOS-ARD-style two-tier; **interim regime, stated honestly: Phase 1 levels are assigned by the platform team against published criteria — independent certification is a Phase 3 governance deliverable**), authority (nationally-recognized vs global-fallback — flagged, never silent), **residency** (platform / external call-out / hub-local), staleness with declared last-good. | Datasets are **registry rows, never tools**. Upstreams: GEOGLAM, CHIRPS/CHIRTS/ERA5, GAEZ statics, corpus documents, hub endpoints. | ✅ core (corpus search/documents, GEOGLAM client); climate feeds pending hub lists |
| 3 | **compute** | Registered deterministic analytics — versioned, documented, sandboxed. The LLM never does arithmetic; it invokes named formulas. Numeric/policy claims (threshold exceedance, calendar windows) check via constraint validators, not LLM judgment. | Hub-contributed formulas enter through a **generification pipeline** (local → reviewed → shared-core, DHIS2-App-Hub-style). The pipeline requirement **activates when contribution opens broadly (Phase 3)**; the first flagship analytics (the S2S pipeline, the PoC thresholds) are **hand-reviewed, as the pipeline's design input** — not an exemption, its prototype. | ◐ (risk weights + calendar phases exist inline; registry is new) |
| 4 | **context** | Owned human judgments as first-class data: crop calendars, thresholds, weights, authority choices, local language defaults. Swappable per request; every swap declared (rule 4). | "Everything is swappable" made safe: config, never code (the DHIS2-vs-OpenMRS lesson — hubs must never patch core to localize). | ✅ (calendar w/ target-pinned overrides; generalizes) |
| 5 | **assemble** | The evidence pack: deterministic, citable, gap-declaring, minted with a `pack_id` the server remembers. The seam where the consumer's LLM takes over. | Declared gaps are content ("no sub-national yield data — said so"). | ✅ (`gather_evidence`, needs a public face) |
| 6 | **verify** | The gate: deterministic groundedness of any draft against a pack — citations resolve, paragraphs sourced, numbers exist in evidence. **Correction mode** returns a minimally re-grounded draft with flagged spans (gate as feature, not blocker). | Free and automatic, always. We *admit* checking is commoditized (hyperscalers sell it for cents); the moat is what it checks against and what it feeds. **Sell the loop, never the check.** | ✅ (`check_grounded`; correction mode new) |
| 7 | **record** | The receipt: auto-minted as a side effect of every pack and verify call — never a separate step. **Durable short links** that survive paste into a cabinet memo (not strippable metadata). **Claim-scoped with brutal precision**: "verified = claims traceable to pack X at time T under gate vN; NOT verified = truth of the pack's sources." Evidence-tiered (rule 6). **Center-blind** for hub-local data (hub-signed digests; see the residency caveat in §1). Append-only public hash log. Contributors get a **data tracker** (platform-hosted and call-out tiers). | The institution — and a network good by our own research, shipped early anyway because it has documented **single-player value: developer observability** (hubs debug their compositions against receipts). The gatekeeper channel (§7) is what upgrades it from debugging aid to institution. | ◐ (receipts per-answer; persistence + durable links = Phase 1 minimum; log, tiering staged) |
| 8 | **contribute** | The door in, for every noun the bones consume: documents, datasets/pointers, analytics, context — all under provenance enforcement (refuses without source + date + validation status). **Branded credit** on every pack/brief/receipt ("data: Hub X, validation L2") + free widget rendering of contributed data — the HDX services-for-data mechanic that seeds supply before any network exists. Free identity/signing onboarding for small agencies. | CLA keeps contributions in the commons if a contributor is acquired. | ✅ (provenance-enforced ingest); registry breadth new |

**Tool-count discipline — derived, not asserted.** The bones map to **15 tools** (16 with the
v1.5 copy-in recipes), and the count is **flat in the number of packs**:

| Bone | Tools |
|---|---|
| discover | `platform.capabilities` (1) |
| resolve | `resolve.place_time` (2) |
| fetch | `corpus.search` (3) · `corpus.document` (4, list+get) · `feeds.query` (5 — live feeds; *dataset is a parameter*) |
| compute | `compute.run` (6 — *analytic is a parameter*; registry listed via capabilities) |
| context | `context.get` (7 — calendars/thresholds, override semantics) |
| assemble | `assemble.pack` (8) |
| verify | `verify.groundedness` (9, correction mode included) |
| record | `record.receipt` (10, mint+resolve) |
| contribute | `contribute.submit` (11 — one door; *kind* is a parameter) |
| compositions | `compose.run` (12 — **one generic invoker; pack compositions are registry rows**, so `foodsecurity.brief` is a row, not a tool, and new packs add zero tools) |
| UI | `ui.design` (13) · `ui.catalog` (14) · `ui.embed` (15) · `ui.component` (16, v1.5) |

Schemas stay terse (total footprint in the low thousands of tokens), dynamic discovery
supported, every tool cleanly callable from code-execution harnesses. New datasets, analytics,
widgets, and compositions are **registry rows served by existing tools, never new tools** — the
survival profile the MCP-sprawl research selects for.

---

## 3. Domain Packs — the product surface

**We do not ship 13 raw tools; raw tool surfaces have no product-market fit** (the
ChatGPT-plugins lesson). The discovery and adoption surface is the **Pack**: a versioned,
importable bundle — the WHO-metadata-package pattern from DHIS2:

> **Pack = registered sources (passported) + calendars/context + analytics + skills + widgets +
> a reference composition + seeded demo receipts.**

- **Food-Security Pack — staged honestly** (the flagship, and the anchor the graveyard demands),
  built around the hub-locked PoC question — *early signs of maize failure in the current
  season, Kenya (Trans-Nzoia) vs Zambia*:
  - **v0 (Phase 1 — what can actually ship now):** registered sources (GEOGLAM conditions, the
    El Niño corpus — 62 docs, archived) + hub crop calendars + the early-warning composition +
    seeded demo receipts + a minimal skill. Analytics and widgets are **declared gaps in the
    pack manifest** — the platform's own gap-declaring ethos applied to itself.
  - **v1 (Phase 2):** + climate feeds as hub data lists arrive (CHIRPS rainfall/forecast,
    CHIRTS/ERA5 temperature, GAEZ/ALOS statics) + hub-supplied threshold analytics + the hub
    S2S pipeline (first hub-contributed asset; hand-reviewed, sets the generification pattern)
    + the brief/calendar/provenance-graph widgets + full skills.
  - **Declared deferral — the acreage component.** The locked PoC expects two quantified
    outputs: rainfall-vs-thresholds (served by v1) and **acreage under drought/flood stress —
    which requires crop-type/land-cover masks and is deferred** until either the hub's
    nationally-recognized map registers (authority-labeled) or a global fallback is used
    (flagged as such, per bone 2). The pack manifest says so; the drop is declared, not silent.
  - **Pillars 2–3 (crop-monitoring depth, yield forecasting / outlook-to-tonnage decisions):**
    deferred per the sequencing the hubs themselves agreed (climate → crop monitor → yield);
    expected to enter as **hub-contributed analytics via the generification pipeline**, not
    core — yield was flagged by the hubs as the hard pillar, and it is exactly the
    heterogeneous-analytics territory §9 assigns to hubs.
- **Global-Risk Pack**: the existing flood assessment refactored into the bones (absorbing the
  known backend bug list — refactor once).
- **Land-Cover Pack**: future, thin (stats over the hub's nationally-recognized time series);
  only its authority-label lesson is absorbed into the core now.
- **Outbound integration** (the "how does this feed *into* Crop Monitor and existing work
  streams?" ask): platform outputs are consumable the other way too — briefs, packs, and
  receipts flow out through the REST mount and `compose.run` into hub bulletins, dashboards,
  and national work streams; the copy path carries the receipt link. Building those
  integrations is hub-app territory; making outputs consumable is ours.

**Composite tools** (e.g. `foodsecurity.brief`) are pre-wired compositions that live *inside
packs* — conveniences for REST consumers with no LLM in the loop (scheduled bulletins, dashboard
backends), not bones.

---

## 4. The canonical flow (build time, then run time)

**Build time** — the hub developer, in their own Claude:
> "Build me a bulletin tool: every Monday, check early signs of maize failure in Trans-Nzoia and
> Southern Province, write a cited brief under our branding, verification stamp, source
> trace-back, ministry-adjustable calendar."

Their agent reads `capabilities` + the pack's skills, learns the canonical loop, scaffolds their
app against our tools, pulls design tokens, embeds widgets. Twenty minutes later the hub owns a
product we've never seen — that is honest anyway, because the guardrails live below it.

**Run time** — the built app (or an analyst directly, the zero-code degenerate case):
1. `resolve` place/time (Phase 1 ships **resolve-minimal**: country/region + calendar-window
   resolution from existing code; full gazetteer semantics are Phase 2) → 2. `assemble` mints an
   evidence pack (`pack_id`) from registered sources → 3. **their LLM** drafts from the pack →
   4. `verify` gates it (correction mode on failure) and mints the receipt → 5. the answer ships
   with a resolvable receipt link — or ships visibly naked, which the published convention
   defines as "not ours."

Generation is the consumer's by default (their model, their tokens, their phrasing); the
server-side composite path remains for no-LLM consumers. The nightmare consumer — someone
wrapping our chrome around an ungated chatbot — is converted, not fought: one `verify` call gets
them a genuine receipt (honest path cheaper than forgery).

---

## 5. Consumption levels and starter kits

| Level | Who | What they use |
|---|---|---|
| 1 — tools only | hub developers, vibe-coders, anyone's Claude | The MCP server + packs + skills |
| 2 — reference agent | hubs who want answers, not plumbing | Our LangGraph agent behind REST — built to prove the rails and **designed to become the minority client** (the UPI/BHIM reference-app pattern) |
| 3 — reference UI | hubs with no front-end team; demos; training | Our web app, generic-branded — the demo vehicle and the widget source, not an operated product |

The fit assessment is blunt: **starter kits are load-bearing adoption infrastructure, not
extras** — several hubs expect accompanied delivery, and no precedent anywhere shows APIs alone
converting low-capacity partners. Hub enablement (sprints, apprenticeship-style onboarding,
skills) is a funded program line, the HISP lesson.

**Widgets** target the **MCP Apps standard** (Anthropic + OpenAI + Microsoft, Jan 2026): the
brief, adjustable crop calendar, and insight-provenance graph ship as UI resources rendering
natively in Claude/ChatGPT/Copilot, with iframe embeds as fallback — app-grade experience without
owning an app. Trust classes hold: presentational (style freely) / input (emits judgment, never
renders its own verdict) / receipt-bound (fail-closed, server-resolved, `sample:true` renders a
watermark). **Enforcement mechanism inside third-party hosts** (where sandboxes may block
network calls): a receipt-bound widget either fetches verdict state directly from the receipt
resolver, or offline-verifies a **signed receipt** embedded in its payload (the Ed25519 format
§7 adopts) — and when neither is possible, renders "unverified," with the iframe embed as the
fallback surface. "Trust classes hold" is a mechanism, not an assertion.

---

## 6. What we operate (and the economics)

We run **three things**: the hosted server (remote-first, OAuth 2.1, per-user identity
passthrough, structured audit logs — enterprise-gateway-compatible, which is now a procurement
prerequisite), the **registries** (sources, analytics, context, widgets, packs), and the
**receipt institution** (store + open resolver + transparency log + retention).

Residency is three-tier by design: platform-hosted · external call-out · **hub-local** (the
hub's own MCP server composed alongside ours in the same client; our skills teach cross-server
composition; receipts stay center-blind).

**Economics, settled by precedent — stated precisely:** the MCP layer and the **verdict
computation** are free at every tier, forever (no one who charged for the protocol layer
thrived; verdicts price at zero). **What is priced is evidence access, by residency and
privacy** — verify-against-public-evidence is free; verify-against-your-private-registered-data
sits inside the hub subscription. Revenue shape: free public tier (public corpus + public-
evidence verify — the Context7 adoption curve) → **hub subscription** (private data,
contribution capacity, receipt-resolution SLA, hosted operations — the ODK-Cloud/DHIS2-fee
model) → **institutional relying parties** (the certification/insurance channel — see §7).
The **public-good exemption** has published criteria (government, humanitarian, and academic
non-commercial use), not vibes. **Level-2 agent and composite-path inference is a metered cost
inside the subscription** — the one place we run an LLM, priced rather than hidden. Free-tier
**receipt retention is costed**: full evidence retention for a stated window, hash-plus-manifest
archival after it — rule 7 and the pricing model must visibly coexist. Transparent quotas and
pricing from day one (the Earth-Engine trust lesson); who-pays decided *now*, not retrofitted
(the UPI zero-MDR lesson).

---

## 7. The receipt institution (the long game)

The research is unambiguous: receipts become valuable **only when a relying party demands
them** — Chrome for Certificate Transparency, insurers for Truepic, a statute for X-Road.
Consumer demand is *manufactured by a gatekeeper*, then normalized. So:

- **Recruit one "Chrome":** an anticipatory-action or parametric-finance donor, a regional
  secretariat, or a ministry audit office that makes a resolvable receipt link a condition of
  accepted analysis. Money that moves on evidence is the only proven demand channel
  (AIUC-certification / hallucination-insurance market).
- **Build the verifier page before scaling issuance:** the flagship UX is "paste a receipt link
  → plain-language verdict, declared gaps rendered as prominently as confirmations,
  cryptography last." A receipt nobody resolves is theater.
- **Interoperate, don't invent:** adopt the emerging signed-receipt wire format (Ed25519 over a
  canonical hash, offline-verifiable) and emit C2PA-compatible manifests where media is
  involved; differentiate on the one thing pure tool-call receipts can't attest — **content-level
  groundedness against passported evidence**.
- **Survive politics and platforms:** deterministic machine verification (the property whose
  absence got the US federal attestation mandate rescinded), open self-hostable resolution (the
  property whose absence killed ClaimReview), multi-party governance before a second sovereign
  sponsor depends on it (the NIIS lesson), and funding never concentrated in one sponsor (the
  FEWS-NET warning, in our own domain).

---

## 8. Sequencing (single-player first, network goods last)

- **Phase 1 — the Pack proves the bones** (now): mount the existing functions (assemble /
  verify / fetch-corpus / context / resolve-minimal / **record-minimal = persisted receipts
  with durable resolvable links**, per §1) as the MCP server; **Food-Security Pack v0** (the
  Phase-1 profile defined in §3, gaps declared in its manifest) wired to the locked PoC; Claude
  Code build-demo; **measure the toil numbers** ("the gate replaces the manual fact-check
  pass"; "packs cut sourcing from days to minutes") — they become the pitch.
- **Phase 2 — build-out**: full `resolve`; the compute registry + first hub-contributed
  analytic (S2S — hand-reviewed, the generification pipeline's design input); climate feeds →
  Pack v1; MCP Apps widgets; correction mode; skills; **the verifier page** ("paste a receipt
  link → plain-language verdict + gaps") — built *before* issuance scales, per §7.
- **Phase 3 — network goods** (deliberately last): contribution opens broadly at dev sprints,
  the generification pipeline formalizes (activating bone 3's contract requirement), the
  transparency log, independent validation certification (bone 2's target regime), gatekeeper
  recruitment, multi-org resolver governance.

**Health metrics** (the GEOSS anti-lesson — it died slowly and invisibly): hub apps shipped and
in weekly use; per-hub data freshness; hub-developer return rate. Tracked **monthly, each with a
stated intervention threshold that triggers hub outreach** — measuring without triggers is
exactly the slow invisible decline the lesson warns about. Never datasets-available or API-call
counts.

---

## 9. What we are not building

No front ends or portals for beneficiaries (hubs build those; starter kits bridge). No LLMs
shipped, no per-verdict or per-protocol charges. No per-framework component libraries; no
marketplace of our own (official MCP registries + signatures/SBOM). No land-cover generation
(hub capability track). No heterogeneous open-ended analytics in core (hub territory — the ONDC
lesson: uniform semantics or nothing). No verdict-as-prop UI in any packaging, no logo packs, no
suppression of declines/gaps/ADJUSTED — the refuse-list stands.
