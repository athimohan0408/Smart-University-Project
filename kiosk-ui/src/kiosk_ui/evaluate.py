"""End-to-end evaluation: replay the test set through the whole system and write the final report.

Each scenario in ``evaluation/test_set.jsonl`` is one visitor (one session) of 1-2 turns. Every turn goes
through this module's ``POST /api/turn`` -> voice-memory (memory, hand-off) -> rag-core (answer), so the
numbers cover the system the visitor actually uses. Per turn it checks the expected ``outcome``, that every
``keypoints`` string is in the answer, the map route (``route_to``), that no route is shown when there must
not be one (``off_map`` / ``no_route``), ``escalate`` and ``min_options``.

The report (``reports/evaluation_report.md``) adds map coverage and copies in the other modules' reports,
so the whole team's numbers are in one place.

Run (rag-core and voice-memory APIs running): ``python -m kiosk_ui.evaluate [--url http://127.0.0.1:8002]``.
Without ``--url`` the kiosk app runs in-process and only voice-memory's API must be up.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from kiosk_ui.analytics import timing
from kiosk_ui.config import Settings, get_settings
from kiosk_ui.eventlog import read_jsonl
from kiosk_ui.navigation import FloorMap, normalise_place_id


def check_turn(expect: dict, result: dict) -> list[str]:
    """Return the failed checks for one turn (empty = pass)."""
    turn, routes = result["turn"], result.get("routes") or []
    answer = (turn.get("answer") or "").casefold()
    outcome = turn.get("outcome")
    shown = {normalise_place_id(r["place_id"]) for r in routes}
    fails = []
    allowed = expect.get("outcome")
    if allowed is not None:
        allowed = [allowed] if isinstance(allowed, str) else allowed
        if outcome not in allowed:
            fails.append(f"outcome {outcome!r}, expected {' or '.join(allowed)}")
    missing = [k for k in expect.get("keypoints", []) if k.casefold() not in answer]
    if missing:
        fails.append(f"answer is missing {missing}")
    if (want := expect.get("route_to")) and normalise_place_id(want) not in shown:
        fails.append(f"no route to {want} (routes shown: {sorted(shown) or 'none'})")
    if (want := expect.get("route_to_if_answered")) and outcome == "answered" and normalise_place_id(want) not in shown:
        fails.append(f"answered but no route to {want}")
    if (expect.get("off_map") or expect.get("no_route")) and shown:
        fails.append(f"a route was shown ({sorted(shown)}) where none should be")
    if "escalate" in expect and bool(turn.get("escalate")) != expect["escalate"]:
        fails.append(f"escalate={turn.get('escalate')}, expected {expect['escalate']}")
    if (n := expect.get("min_options")) and len(turn.get("options") or []) < n:
        fails.append(f"{len(turn.get('options') or [])} option buttons, expected at least {n}")
    return fails


def run(test_set: list[dict], post) -> list[dict]:
    """``post(path, json) -> dict``. Returns one record per turn."""
    records = []
    stamp = datetime.now().strftime("%H%M%S")
    for scenario in test_set:
        session = f"eval-{scenario['id']}-{stamp}-{uuid.uuid4().hex[:4]}"
        for i, t in enumerate(scenario["turns"]):
            start = time.perf_counter()
            try:
                result = post("/api/turn", {"session_id": session, "text": t["say"]})
                error = None
            except Exception as exc:  # noqa: BLE001 - record and carry on with the next scenario
                result, error = {"turn": {}, "routes": []}, f"{type(exc).__name__}: {exc}"
            elapsed = round((time.perf_counter() - start) * 1000, 1)
            fails = [f"request failed: {error}"] if error else check_turn(t["expect"], result)
            turn = result["turn"]
            records.append({
                "scenario": scenario["id"], "category": scenario["category"], "turn": i + 1, "say": t["say"],
                "expect": t["expect"], "outcome": turn.get("outcome"), "answer": turn.get("answer"),
                "escalate": turn.get("escalate"), "escalate_reason": turn.get("escalate_reason"),
                "options": turn.get("options") or [], "routes": [r["place_id"] for r in result.get("routes") or []],
                "round_trip_ms": elapsed, "upstream_total_ms": (turn.get("latency_ms") or {}).get("total"),
                "passed": not fails, "fails": fails,
            })
        try:
            post("/api/session/reset", {"session_id": session})
        except Exception:  # noqa: BLE001
            pass
    return records


def map_coverage(fmap: FloorMap, faculty_json: Path) -> dict:
    """8th-floor rooms in the faculty data vs rooms the map can route to."""
    routable = {p.id.casefold() for p in fmap.places if p.routable}
    rooms: set[str] = set()
    if faculty_json.exists():
        for fac in json.loads(faculty_json.read_text(encoding="utf-8")).get("faculty", []):
            for loc in _locations(fac):
                room = str(loc.get("room") or "")
                if room and _on_this_floor(loc):
                    rooms.add(f"room:{room.casefold()}")
    return {"faculty_rooms_on_floor": sorted(rooms), "missing_from_map": sorted(rooms - routable),
            "routable": len(routable), "source": str(faculty_json) if faculty_json.exists() else None}


def _locations(fac: dict) -> list[dict]:
    locs = fac.get("locations") or fac.get("location") or []
    return locs if isinstance(locs, list) else [locs]


def _on_this_floor(loc: dict) -> bool:
    for key in ("on_8th_floor", "on_this_floor"):
        if key in loc:
            return bool(loc[key])
    text = " ".join(str(v) for v in loc.values()).casefold()
    return "central block" in text and ("eighth" in text or "8th" in text)


def _pct(n: int, d: int) -> str:
    return f"{n / d:.1%} ({n}/{d})" if d else "n/a"


def to_markdown(records: list[dict], settings: Settings, fmap: FloorMap, coverage: dict) -> str:
    scenarios: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        scenarios[r["scenario"]].append(r)
    s_pass = sum(all(t["passed"] for t in ts) for ts in scenarios.values())
    t_pass = sum(r["passed"] for r in records)

    nav = [r for r in records if r["expect"].get("route_to")]
    nav_ok = sum(1 for r in nav if normalise_place_id(r["expect"]["route_to"]) in
                 {normalise_place_id(x) for x in r["routes"]})
    no_route = [r for r in records if r["expect"].get("off_map") or r["expect"].get("no_route")]
    no_route_ok = sum(1 for r in no_route if not r["routes"])

    esc_expected = [r for r in records if r["expect"].get("escalate") is True]
    esc_done = [r for r in records if r["escalate"]]
    esc_tp = sum(1 for r in esc_expected if r["escalate"])
    answer_expected = [r for r in records if r["expect"].get("outcome") == "answered"]
    wrongly_escalated = sum(1 for r in answer_expected if r["escalate"])

    rt = timing([r["round_trip_ms"] for r in records])
    up = timing([r["upstream_total_ms"] for r in records if r["upstream_total_ms"] is not None])

    lines = [
        "# Final evaluation report",
        "",
        f"Generated {datetime.now().isoformat(timespec='seconds')} by `python -m kiosk_ui.evaluate` "
        f"from `{settings.evaluation.test_set.name}` ({len(scenarios)} scenarios, {len(records)} turns).",
        "Every turn went through the kiosk (Portion 3) -> voice-memory (Portion 2) -> rag-core (Portion 1).",
        "",
        "## Summary (end to end)",
        "",
        "| measure | result |",
        "|---|---|",
        f"| scenarios fully correct | {_pct(s_pass, len(scenarios))} |",
        f"| turns correct | {_pct(t_pass, len(records))} |",
        f"| navigation: correct route shown | {_pct(nav_ok, len(nav))} |",
        f"| navigation: no route where none should be | {_pct(no_route_ok, len(no_route))} |",
        f"| escalations that should happen and did | {_pct(esc_tp, len(esc_expected))} |",
        f"| answerable questions wrongly escalated | {_pct(wrongly_escalated, len(answer_expected))} |",
        f"| all escalations in the run | {len(esc_done)} |",
    ]
    if rt:
        lines.append(f"| response time at the kiosk (typed) | mean {rt.mean} ms, p50 {rt.p50} ms, p95 {rt.p95} ms |")
    if up:
        lines.append(f"| voice-memory turn total | mean {up.mean} ms, p95 {up.p95} ms |")

    lines += ["", "## By category", "", "| category | turns | correct |", "|---|---|---|"]
    cats: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        cats[r["category"]].append(r)
    for c, rs in sorted(cats.items()):
        lines.append(f"| {c} | {len(rs)} | {_pct(sum(r['passed'] for r in rs), len(rs))} |")

    failed = [r for r in records if not r["passed"]]
    lines += ["", "## Failed turns", ""]
    if not failed:
        lines.append("None.")
    for r in failed:
        lines += [f"- **{r['scenario']} turn {r['turn']}** ({r['category']}): \"{r['say']}\"",
                  f"  - got `{r['outcome']}`: {r['answer']!r}",
                  *(f"  - {f}" for f in r["fails"])]

    lines += ["", "## Floor map", "",
              f"- Map: `{settings.map.file.name}`, {coverage['routable']} routable places, "
              f"verified: **{'yes, by ' + str(fmap.verified_by) if fmap.verified else 'NO (placeholder layout)'}**."]
    if coverage["source"]:
        lines.append(f"- 8th-floor rooms in the faculty data: {len(coverage['faculty_rooms_on_floor'])}; "
                     f"missing from the map: {', '.join(coverage['missing_from_map']) or 'none'}.")
    else:
        lines.append("- Faculty data not built yet (`cd rag-core; just index`), so room coverage was not checked.")
    if not fmap.verified:
        lines.append("- Route lengths and left/right directions are only as good as the placeholder coordinates.")

    lines += ["", "## Kiosk usage (from the analytics log)", ""]
    ui = read_jsonl(settings.logs.ui_events)
    lines.append(f"{len(ui)} kiosk events logged so far (includes this evaluation run).")

    lines += ["", "## Numbers from the other modules", ""]
    for title, path in _module_reports(settings):
        body = path.read_text(encoding="utf-8").strip()
        lines += [f"### {title} (`{path.name}`)", "", _demote(body), ""]
    return "\n".join(lines).rstrip() + "\n"


def _module_reports(settings: Settings) -> list[tuple[str, Path]]:
    out = []
    rag_dir = settings.evaluation.rag_reports
    if rag_dir.is_dir():
        for p in sorted(rag_dir.glob("*.md")):
            if p.name != "data_requests.md":
                out.append(("rag-core", p))
    if settings.evaluation.voice_report.exists():
        out.append(("voice-memory", settings.evaluation.voice_report))
    return out


def _demote(markdown: str) -> str:
    """Push headings down three levels so a copied report nests under its ### title."""
    return "\n".join("###" + line if line.startswith("#") else line for line in markdown.splitlines())


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="End-to-end evaluation through the kiosk and the final report.")
    parser.add_argument("--url", default=None, help="a running kiosk-ui server; default: run it in-process")
    parser.add_argument("--test-set", type=Path, default=settings.evaluation.test_set)
    parser.add_argument("--out", type=Path, default=settings.evaluation.report)
    parser.add_argument("--faculty-json", type=Path,
                        default=settings.evaluation.rag_reports.parent / "data" / "processed" / "faculty.json")
    args = parser.parse_args(argv)

    test_set = read_jsonl(args.test_set)
    if args.url:
        import httpx

        client = httpx.Client(base_url=args.url, timeout=60)
    else:
        from fastapi.testclient import TestClient

        from kiosk_ui.server import create_app

        client = TestClient(create_app(settings))

    def post(path: str, body: dict) -> dict:
        response = client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    with client:
        health = client.get("/api/health").json()
        if not isinstance(health.get("voice_memory"), dict):
            print(f"voice-memory is not reachable ({health.get('voice_memory')}). Start it first: "
                  "cd voice-memory; python -m voice_memory.api")
            return 2
        records = run(test_set, post)

    fmap = FloorMap.load(settings.map.file)
    report = to_markdown(records, settings, fmap, map_coverage(fmap, args.faculty_json))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    settings.evaluation.results.parent.mkdir(parents=True, exist_ok=True)
    settings.evaluation.results.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                                           encoding="utf-8")
    passed = sum(r["passed"] for r in records)
    print(f"{passed}/{len(records)} turns correct. Report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
