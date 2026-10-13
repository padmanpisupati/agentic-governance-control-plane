"""Optional: agcp guarded tools inside a LangGraph StateGraph.

    PYTHONPATH=src:. python -m examples.langgraph_integration.run [--no-langgraph]

The graph never touches the ERP directly; its "act" node calls a GuardedAction and branches on
the decision outcome. Without langgraph installed, the same nodes run in a plain sequential loop.
"""
from __future__ import annotations

import argparse
from typing import TypedDict

from agcp import PolicyDenied, correlation
from examples.order_exception.run import AGENT, build, triage


class State(TypedDict, total=False):
    order_id: str
    proposal: tuple
    outcome: str
    approval_id: str


def make_nodes(erp, A):
    def propose(s: State) -> State:
        return {"proposal": triage(erp.get(s["order_id"]))}

    def act(s: State) -> State:
        kind, params, conf = s["proposal"]
        try:
            r = A[kind](actor=AGENT, confidence=conf, **params)
            return {"outcome": r.status, "approval_id": r.approval_id}
        except PolicyDenied as e:
            return {"outcome": f"DENY {e.decision.rule_ids}"}

    def route(s: State) -> str:
        return "hitl" if s["outcome"] == "PENDING_APPROVAL" else "end"

    def hitl(s: State) -> State:  # in production: interrupt() and resume on the human's answer
        return {"outcome": f"PENDING_APPROVAL -> parked as {s['approval_id']}"}

    return propose, act, route, hitl


def run(order_ids, use_langgraph=True):
    cp, erp, clock, A = build()
    propose, act, route, hitl = make_nodes(erp, A)
    engine = "sequential-fallback"
    invoke = None
    if use_langgraph:
        try:
            from langgraph.graph import END, START, StateGraph
            g = StateGraph(State)
            g.add_node("propose", propose)
            g.add_node("act", act)
            g.add_node("hitl", hitl)
            g.add_edge(START, "propose")
            g.add_edge("propose", "act")
            g.add_conditional_edges("act", route, {"hitl": "hitl", "end": END})
            g.add_edge("hitl", END)
            invoke, engine = g.compile().invoke, "langgraph"
        except ImportError:
            pass
    if invoke is None:
        def invoke(s):
            s = {**s, **propose(s)}
            s = {**s, **act(s)}
            return {**s, **hitl(s)} if route(s) == "hitl" else s
    print(f"== LangGraph integration | engine={engine}")
    outs = []
    for oid in order_ids:
        with correlation(f"lg-{oid}"):
            out = invoke({"order_id": oid})
        outs.append(out)
        print(f"  {oid}: {out['proposal'][0]:<8} -> {out['outcome']}")
    print(f"  audit chain valid={cp.audit.verify().ok}, records={len(cp.audit.records())}")
    return engine, outs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-langgraph", action="store_true")
    a = ap.parse_args(argv)
    return run(["SO-70000", "SO-70002", "SO-70005", "SO-70016"], use_langgraph=not a.no_langgraph)


if __name__ == "__main__":
    main()
