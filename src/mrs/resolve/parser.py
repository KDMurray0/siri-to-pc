"""One request, one parser, one owner.

The old flow ran the local grammar *and* Groq over the same phrase in sequence,
so it was never obvious which one had decided anything. The rule now:

  1. An exact transport/volume phrase ("pause", "skip", "volume 40") is decided
     locally and instantly — no reason to spend 400ms and a token budget on it.
  2. Everything else goes to the LLM when it's available.
  3. The grammar is the fallback, used when the LLM is off, rate-limited or
     confused.

Whatever decides sets `plan.via`, which is logged and shown in diagnostics, so
"is it using Groq?" is answerable at a glance.
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
