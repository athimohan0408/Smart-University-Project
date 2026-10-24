"""CLI: ``python -m rag_core "where is Dr Nisha Varghese's cabin?"`` prints the RAGResult.

Options: ``--now 2026-10-20T14:30`` pins the clock, ``--context '{"last_entities": [...]}'`` passes
dialogue context, ``--json`` prints the raw RAGResult JSON, ``--log`` appends to the query log.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_core", description="Ask the 8th-floor receptionist.")
    parser.add_argument("question", nargs="+", help="the (resolved) visitor question")
    parser.add_argument("--now", type=datetime.fromisoformat, help="ISO-8601 local time for 'now'/'today'")
    parser.add_argument("--context", type=json.loads, default=None, help="JSON object of dialogue context")
    parser.add_argument("--json", action="store_true", help="print the RAGResult as JSON")
    parser.add_argument("--log", action="store_true", help="append this request to paths.query_log")
    args = parser.parse_args(argv)

    from rag_core.config import get_settings
    from rag_core.pipeline import Pipeline

    pipe = Pipeline.from_settings(get_settings(), log_queries=args.log)
    trace = pipe.run(" ".join(args.question), args.context, args.now)
    r = trace.result
    if args.json:
        print(r.model_dump_json(indent=2))
        return 0
    print(r.answer)
    print()
    status = f"ESCALATE ({r.escalate_reason})" if r.escalate else "answered"
    print(f"  {status}   confidence {r.confidence:.2f}   intent {r.intent.value} ({r.intent_confidence:.2f})"
          f"   answer source: {trace.generation.source}")
    for e in r.entities:
        print(f"  entity  {e.type.value:9s} {e.text!r} -> {e.canonical_id} ({e.score:.2f})")
    print(f"  cited   {', '.join(r.sources) or '-'}")
    print(f"  fetched {', '.join(trace.retrieved_ids) or '-'}")
    if trace.grounding.issues:
        print(f"  grounding issues: {', '.join(f'{i.kind}: {i.span}' for i in trace.grounding.issues)}")
    print("  latency " + ", ".join(f"{k} {v:.0f} ms" for k, v in r.latency_ms.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
