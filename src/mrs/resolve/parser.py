"""One parser, one decision.

Exact controls (pause, skip, volume) are handled locally so they're instant.
Everything else goes to the LLM, with the grammar as fallback. Whoever decided
sets plan.via.
"""

from __future__ import annotations

from ..logging_setup import get
from ..models import Plan
from . import grammar, llm

log = get("parse")


def parse(text: str, *, mode: str = "play") -> Plan:
    text = (text or "").strip()
    if not text:
        return Plan(kind="none", spoken=text)

    # 1. instant local controls
    cmd = grammar.transport(text)
    if cmd:
        return Plan(kind="command", command=cmd, via="grammar", spoken=text,
                    mode=mode)
    vol = grammar.volume_intent(text)
    if vol:
        return Plan(kind="command", command=vol[0], query=str(vol[1]),
                    via="grammar", spoken=text, mode=mode)
    listname = grammar.playlist_add(text)
    if listname:
        return Plan(kind="command", command="add_to_playlist", query=listname,
                    via="grammar", spoken=text, mode=mode)

    # 2. the LLM, when we have one
    if llm.available():
        plan = llm.parse(text)
        if plan:
            plan.mode = mode
            log.info("llm: %r -> %s %r (artist=%r)", text, plan.kind, plan.query,
                     plan.artist)
            return plan

    # 3. grammar fallback
    plan = grammar.parse(text)
    plan.mode = mode
    log.info("grammar: %r -> %s %r", text, plan.kind, plan.query)
    return plan
