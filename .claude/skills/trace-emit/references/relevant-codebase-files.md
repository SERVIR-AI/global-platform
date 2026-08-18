# Where this pattern came from

A map of the originating implementation, for the case where the user has it checked out and
asks to see it. **Do not read these unless asked.** Nothing in this skill needs them, and the
host project will not look like this.

Repo: `metadata.origin` in `SKILL.md`. Stack: FastAPI + LangGraph, Python.

| Path | What is in it |
| --- | --- |
| `apps/api/src/app/graph/tracing.py` | All of it - every step builder, the envelope assembler, the writer. ~500 lines. |
| `apps/api/src/app/graph/graph.py` | The graph nodes. Each times itself and calls its builder before returning. |
| `apps/api/src/app/graph/geo/ingest.py` | Emission sites - external calls and cache checks calling `emit`. |
| `apps/api/src/app/api/routes/chat.py` | The turn boundary: assemble, persist, attach, inside a bare `except`. |
| `apps/api/tests/test_tracing.py` | Builders unit-tested directly, no pipeline run. |
| `apps/api/tests/test_trace_recovery.py` | Failure and recovery behaviour asserted end to end. |

Read it as one instantiation, not as a template. `worked-example.md` covers the same ground
from the captured output, which is usually the better place to start.
