"""Route -> run the deterministic tool -> write the answer -> record the trace."""
from . import llm, tools, trace


def ask(question):
    routed = llm.route(question)
    if "text" in routed:                       # model declined to call a tool
        return trace.record(question, routed["text"], [routed["usage"]])
    try:
        result = tools.dispatch(routed["tool"], routed["args"])
    except Exception as e:                      # unresolvable place, missing data, etc.
        return trace.record(question, f"No data for that request: {e}",
                            [routed["usage"]], args=routed["args"])
    answer, usage = llm.write(question, result, routed)
    return trace.record(question, answer, [routed["usage"], usage],
                        result=result, args=routed["args"])
