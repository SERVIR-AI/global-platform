"""Maize Situation Desk — backend.

Serves the frontend (static files) and a single API endpoint that runs the
platform's full governed workflow for each question:

    assemble_pack -> draft (claude -p) -> verify_groundedness -> record_receipt

The browser only ever talks to this backend; this backend is the only thing
that talks to the GRP MCP server.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from drafting import draft_brief
from grp_client import grp_session

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

CROP = "maize"
ALLOWED_COUNTRIES = {"kenya", "zambia"}

app = FastAPI(title="Maize Situation Desk")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    country: str
    question: str


class AskResponse(BaseModel):
    status: str  # "ok" | "declined"
    note: str | None = None
    country: str | None = None
    crop: str | None = None
    question: str | None = None
    brief: str | None = None
    citations: list | None = None
    gaps: list | None = None
    required_sections: list | None = None
    pack_id: str | None = None
    report_id: str | None = None
    receipt_id: str | None = None
    passed: bool | None = None
    evidence_tier: str | None = None
    failures: list | None = None


@app.post("/api/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    country = req.country.strip().lower()
    question = req.question.strip()

    if country not in ALLOWED_COUNTRIES:
        return AskResponse(status="declined", note=f"unsupported country '{req.country}' — choose kenya or zambia")
    if not question:
        return AskResponse(status="declined", note="a question is required")

    async with grp_session() as grp:
        pack = await grp.call("assemble_pack", country=country, crop=CROP, focus=question)
        if pack.get("status") != "ok":
            return AskResponse(status="declined", note=pack.get("note", "the evidence pack could not be assembled"))

        try:
            brief = await draft_brief(pack, question, country, CROP)
        except Exception as exc:  # drafting failure is a decline, not a crash
            return AskResponse(status="declined", note=f"drafting failed: {exc}")

        report = await grp.call("verify_groundedness", draft=brief, pack_id=pack["pack_id"])
        if report.get("status") != "ok":
            return AskResponse(status="declined", note=report.get("note", "the draft could not be verified"))

        if not report.get("passed"):
            return AskResponse(
                status="declined",
                note="the groundedness gate failed: " + "; ".join(report.get("failures", []) or ["ungrounded draft"]),
                pack_id=pack["pack_id"],
                report_id=report.get("report_id"),
                citations=pack.get("citations"),
                gaps=pack.get("gaps"),
                required_sections=pack.get("required_sections"),
                passed=False,
                evidence_tier=report.get("evidence_tier"),
                failures=report.get("failures"),
            )

        receipt = await grp.call(
            "record_receipt",
            pack_id=pack["pack_id"],
            report_id=report["report_id"],
            question=question,
        )
        if receipt.get("status") != "ok":
            return AskResponse(status="declined", note=receipt.get("note", "the receipt could not be minted"))

        return AskResponse(
            status="ok",
            country=country,
            crop=CROP,
            question=question,
            brief=brief,
            citations=pack.get("citations"),
            gaps=pack.get("gaps"),
            required_sections=pack.get("required_sections"),
            pack_id=pack["pack_id"],
            report_id=report["report_id"],
            receipt_id=receipt.get("receipt_id"),
            passed=True,
            evidence_tier=report.get("evidence_tier"),
        )


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
