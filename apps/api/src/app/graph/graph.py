"""The LangGraph application.

This is intentionally minimal — a single node that calls the chat model — so it's
a clean starting point. Build out from here: add tool nodes, conditional edges,
retrieval, human-in-the-loop, etc.

Conversation memory is handled by the checkpointer: pass a `thread_id` in the run
config and the graph persists/loads message history for that thread.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import MessagesState

from ..config import get_settings
from ..llm import get_chat_model

class InputState(MessagesState):
    pass

def _call_model(state: InputState, config) -> dict:
    """Single agent node: invoke the configured chat model on the message history."""
    configurable = config.get("configurable", {})
    provider = configurable.get("provider")
    model = configurable.get("model")

    llm = get_chat_model(provider=provider, model=model)

    messages = state["messages"]
    settings = get_settings()
    if settings.system_prompt:
        messages = [SystemMessage(content=settings.system_prompt), *messages]

    response = llm.invoke(messages)
    return {"messages": [response]}

def router_node(state: InputState):
    pass

def _build_graph():
    builder = StateGraph(InputState)
    builder.add_node("agent", _call_model)
    builder.add_edge(START, "agent")
    # `MessagesState` + checkpointer gives multi-turn memory keyed by thread_id.
    # MemorySaver is in-process; swap for a persistent checkpointer (Postgres,
    # SQLite) when you need durability across restarts.
    return builder.compile(checkpointer=MemorySaver())


@lru_cache
def get_graph():
    """Compiled graph singleton — compiled once, reused across requests."""
    return _build_graph()
