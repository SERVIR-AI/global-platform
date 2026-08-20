"""Drafts a governed brief from an evidence pack using `claude -p`.

This is the "YOUR LLM drafts the brief" seam of the platform's canonical
loop: assemble_pack -> [draft here] -> verify_groundedness -> record_receipt.
No MCP tool runs an LLM on our behalf for this path.
"""

import asyncio
import json


def _format_citation(c: dict) -> str:
    return (
        f"[{c['n']}] {c.get('source', '?')} — {c.get('title', '')} "
        f"({c.get('pub_date', 'undated')}, validation: {c.get('validation') or 'n/a'})\n"
        f"    {c.get('text', '')}"
    )


def build_prompt(pack: dict, question: str, country: str, crop: str) -> str:
    citations = "\n\n".join(_format_citation(c) for c in pack.get("citations", []))
    gaps = "\n".join(f"- {g}" for g in pack.get("gaps", [])) or "- none declared"
    sections = "\n".join(pack["required_sections"])

    return f"""You are drafting a governed food-security brief for the SERVIR Global Risk Platform.

Question asked: {question}
Country: {country}
Crop: {crop}

Write the brief using EXACTLY these section headers, in this order, and no others
(do not add a "Sources" section or any other section):
{sections}

Rules:
- Every paragraph in every section must cite at least one evidence item inline as [n].
- Only use citation numbers [n] that appear in the evidence pack below. Never invent one.
- Do not write your own "Sources" or "References" section — citations are inline [n] only.
- Address the declared gaps below under "What's missing and how to weigh it".
- Be concise, factual, and do not speculate beyond what the evidence supports.
- Output only the brief itself (the section headers and their prose), nothing else.

Evidence pack (cite by [n]):
{citations}

Declared gaps in this pack:
{gaps}

Write the brief now.
"""


async def draft_brief(pack: dict, question: str, country: str, crop: str) -> str:
    prompt = build_prompt(pack, question, country, crop)

    proc = await asyncio.create_subprocess_exec(
        "claude",
        "-p",
        "--allowedTools",
        "",
        "--output-format",
        "text",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(prompt.encode("utf-8"))

    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed ({proc.returncode}): {stderr.decode('utf-8', 'replace')}")

    return stdout.decode("utf-8").strip()
