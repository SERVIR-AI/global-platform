# Where this pattern came from

A map of the originating implementation, for the case where the user has it checked out and
asks to see it. **Do not read these unless asked.** Nothing in this skill needs them, and they
are typed against one specific backend.

Repo: `metadata.origin` in `SKILL.md`. Stack: React + TypeScript.

| Path | Layer | What is in it |
| --- | --- | --- |
| `apps/web/src/types/trace.ts` | L1 | The discriminated union and per-step interfaces |
| `apps/web/src/lib/trace/parse.ts` | L1 | `parseEnvelope`, and the null-never-throw contract |
| `apps/web/src/lib/trace/selectors.ts` | L2 | Envelope summary, step rows, `stepUsedModel`, formatters |
| `apps/web/src/lib/trace/fields.ts` | L2 | Field descriptors and the audience tag. The largest file. |
| `apps/web/src/lib/trace/labels.ts` | L2 | Every string the frontend invents |
| `apps/web/src/lib/trace/graphTopology.ts`, `graphPath.ts` | L2 | Node/edge geometry as data, and which nodes a turn touched |
| `apps/web/src/components/Trace/` | L3 | Panel, summary, step list, step detail, field value, graph, error boundary |
| `apps/web/src/lib/trace/README.md` | - | Documents the above in full, with a task-to-file map |
| `docs/TRACE_USE_CASES.md` | - | What the trace is good for, and where it falls short |

The structure transfers; the field knowledge does not.
