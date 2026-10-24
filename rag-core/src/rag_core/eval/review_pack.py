"""Build the package for an independent HUMAN review of the gold sets.

Run: ``python -m rag_core.eval.review_pack``  (``just review-pack``) -> ``reports/human_review/``

- ``qa_review.csv``: every QA gold item (dev + frozen test) with its gold facts written out in full and
  empty reviewer columns.
- ``retrieval_review.csv``, ``entities_review.csv``: the other two gold sets, same idea.
- ``system_answers.csv``: what the system (template mode) answered, for the paraphrase question only. Kept
  in a separate file so it doesn't anchor the first pass.

CSV files are UTF-8 with BOM so Excel opens them correctly. The instructions are in README.md there.
Regenerating overwrites the CSVs: copy a filled-in file elsewhere before re-running.
"""

from __future__ import annotations

import csv
import glob
import json
from pathlib import Path

from rag_core.config import PROJECT_ROOT, get_settings

OUT = PROJECT_ROOT / "reports" / "human_review"
QA_REVIEW_COLS = [
    "R1 facts correct? (Y/N/?)",
    "R2 answerable flag correct? (Y/N)",
    "R3 keypoints required and sufficient? (Y/N + fix)",
    "R4 acceptable paraphrases a correct answer might use",
    "R5 name handling right? (similar/ambiguous/speech-to-text; Y/N/na)",
    "R6 intent label right? (Y/N + correct label)",
    "R7 notes",
]


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _keypoints(keys) -> str:
    return " ; ".join(" | ".join(k) if isinstance(k, list) else k for k in keys)


def _entities(ents) -> str:
    out = []
    for e in ents:
        if e.get("ambiguous"):
            out.append(f"{e['type']}: AMBIGUOUS {' / '.join(e['ambiguous'])}")
        elif e.get("id"):
            out.append(e["id"])
        else:
            out.append(f"{e['type']}: \"{e.get('text')}\" (NOT in the records)")
    return " ; ".join(out)


def _write(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def build() -> list[Path]:
    s = get_settings()
    OUT.mkdir(parents=True, exist_ok=True)
    facts = {f["fact_id"]: f["text"] for f in _rows(s.paths.facts_jsonl)}
    written = []

    qa = _rows(s.paths.eval_testset)
    header = ["id", "split", "category", "difficulty", "question", "gold intent", "answerable (false = must hand over "
              "to the receptionist or ask a clarifying question with no facts)", "keypoints (all required; a | b = either)",
              "forbidden (must NOT appear)", "clock (now)", "gold entities", "gold fact ids", "gold fact text (the KB)"] + QA_REVIEW_COLS
    rows = []
    for r in qa:
        text = "\n".join(f"[{fid}] {facts.get(fid, '!! MISSING FACT !!')}" for fid in r.get("gold_fact_ids", []))
        rows.append([r["id"], r["split"], r["category"], r["difficulty"], r["question"], r["gold_intent"],
                     "yes" if r["answerable"] else "no", _keypoints(r.get("gold_answer_keypoints", [])),
                     " ; ".join(r.get("forbidden", [])), r.get("now", ""), _entities(r.get("gold_entities", [])),
                     " ; ".join(r.get("gold_fact_ids", [])), text] + [""] * len(QA_REVIEW_COLS))
    _write(OUT / "qa_review.csv", header, rows)
    written.append(OUT / "qa_review.csv")

    rt = _rows(s.paths.retrieval_test)
    _write(OUT / "retrieval_review.csv",
           ["n", "question", "intent", "gold fact ids", "gold fact text", "R1 gold facts answer the question? (Y/N)",
            "R2 a needed fact is missing? (which)", "R3 notes"],
           [[n, r["question"], r["intent"], " ; ".join(r["gold"]),
             "\n".join(f"[{g}] {facts.get(g, '!! MISSING FACT !!')}" for g in r["gold"]), "", "", ""]
            for n, r in enumerate(rt, start=1)])
    written.append(OUT / "retrieval_review.csv")

    et = _rows(s.paths.entity_test)
    _write(OUT / "entities_review.csv",
           ["n", "text", "clock (now)", "gold entities", "R1 entities right? (Y/N + fix)", "R2 notes"],
           [[n, r["text"], r.get("now", ""), _entities(r.get("entities", [])), "", ""] for n, r in enumerate(et, start=1)])
    written.append(OUT / "entities_review.csv")

    answers = {}
    for path in sorted(glob.glob(str(PROJECT_ROOT / "reports" / "eval_*.json"))):
        if path.endswith("_llm.json"):
            continue
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for q in data.get("questions", []):
            answers[q["id"]] = (q["answer"], q["escalate"], q.get("escalate_reason"), Path(path).name)
    _write(OUT / "system_answers.csv",
           ["id", "question", "system answer (template mode, no LLM)", "handed over to receptionist?", "reason", "from run",
            "R1 would you accept this answer? (Y/N + why)"],
           [[r["id"], r["question"], *(answers.get(r["id"], ("(not run)", "", "", ""))), ""] for r in qa])
    written.append(OUT / "system_answers.csv")
    return written


def main() -> None:
    for path in build():
        print(f"wrote {path.relative_to(PROJECT_ROOT).as_posix()}")


if __name__ == "__main__":
    main()
