"""Evaluation integrity: how blind is the frozen test split?

Run: ``python -m rag_core.eval.integrity``  -> ``reports/eval_integrity.md``

1. Overlap: QA test questions that also appear, verbatim or as near copies (rapidfuzz token_sort_ratio
   >= NEAR), in the other gold sets (retrieval, entities, intent train/test). Those sets were used during
   development, so their copies of test questions were effectively seen.
2. Exposure: test items whose failures were viewed while developing fixes (from the session log in
   reports/iterations.md). Fixes were general rules backed by dev cases, but the patterns were seen.
3. The effect: QA accuracy and wrong-person counts on the CLEAN test items (neither copied nor exposed)
   versus the rest, from the latest template-mode test-split eval JSON.
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path

from rapidfuzz import fuzz

from rag_core.config import PROJECT_ROOT, get_settings

NEAR = 85  # token_sort_ratio for "near copy"
# Test items whose failures were looked at during development (reports/iterations.md, iterations 0-7):
# the baseline failure listing (qa024 qa079 qa086 qa090 qa123 qa142 qa146 qa159 qa170 qa174), later
# listings (qa017 qa035 qa107 qa118 qa120 qa143), and the five you named in iteration 7.
EXPOSED = frozenset("qa017 qa024 qa035 qa079 qa086 qa090 qa107 qa118 qa120 qa123 qa142 qa143 qa146 qa159 qa170 qa174".split())
REPORT = PROJECT_ROOT / "reports" / "eval_integrity.md"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s@.]", " ", text.casefold())).strip(" .")


def _load(path: Path, field: str) -> list[str]:
    return [json.loads(line)[field] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+[a-z]?", text.casefold()))


def overlaps(test_rows: list[dict], others: dict[str, list[str]]) -> list[tuple[str, str, str, str, int, str]]:
    """(test id, test question, other set, other question, score, kind). kind: verbatim | near copy |
    same pattern (the numbers differ, e.g. another room, so the content asked about is different)."""
    out = []
    for row in test_rows:
        q = _norm(row["question"])
        for name, questions in others.items():
            for other in questions:
                o = _norm(other)
                score = 100 if o == q else int(fuzz.token_sort_ratio(q, o))
                if score < NEAR:
                    continue
                kind = "verbatim" if score == 100 else (
                    "same pattern" if _numbers(q) != _numbers(o) else "near copy")
                out.append((row["id"], row["question"], name, other, score, kind))
    return out


def lower_bound(correct: int, n: int, alpha: float = 0.05) -> float:
    """Clopper-Pearson two-sided (1 - alpha) lower bound for an accuracy of correct/n."""
    if n == 0 or correct == 0:
        return 0.0
    from scipy.stats import beta

    return float(beta.ppf(alpha / 2, correct, n - correct + 1))


def latest_template_test_run() -> dict | None:
    for path in sorted(glob.glob(str(PROJECT_ROOT / "reports" / "eval_*.json")), reverse=True):
        if path.endswith("_llm.json"):
            continue
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("split") == "test" and data.get("settings", {}).get("mode", "template") == "template":
            return data | {"_path": Path(path).name}
    return None


def build_report() -> str:
    s = get_settings()
    rows = [json.loads(line) for line in s.paths.eval_testset.read_text(encoding="utf-8").splitlines() if line.strip()]
    test = [r for r in rows if r["split"] == "test"]
    others = {
        "retrieval_test": _load(s.paths.retrieval_test, "question"),
        "entities_test": _load(s.paths.entity_test, "text"),
        "intent_train": _load(s.paths.intent_train, "text"),
        "intent_test (held-out)": _load(s.paths.intent_test, "text"),
    }
    hits = overlaps(test, others)
    copied = {h[0] for h in hits if h[5] != "same pattern"}
    pattern_only = {h[0] for h in hits} - copied
    clean = [r["id"] for r in test if r["id"] not in copied and r["id"] not in EXPOSED]

    out = [
        "# Evaluation integrity: how blind is the frozen test split?",
        "",
        f"- Test split: {len(test)} questions (byte-for-byte frozen; fingerprint checked by "
        "`tests/eval/test_qa_eval.py::test_test_split_is_frozen`).",
        f"- **{len(copied)}** test questions have a verbatim or near copy (token_sort_ratio >= {NEAR}, same numbers) in "
        f"another gold set used during development; {len(pattern_only)} more only share a sentence pattern with a "
        f"different room/number (listed, not counted as copies).",
        f"- **{len(EXPOSED)}** test items had their failures viewed while fixes were developed.",
        f"- **{len(clean)}** test items are clean (neither copied nor exposed).",
        "",
        "## Similar questions in other gold sets",
        "",
        "| test id | test question | found in | as | similarity | kind |",
        "|---|---|---|---|---|---|",
    ]
    out += [f"| {i} | {q} | {name} | {o} | {sc} | {kind} |" for i, q, name, o, sc, kind in hits]
    out += [
        "",
        "How they were used: the retrieval gold set drove route changes and served as evidence for an intent-data fix "
        "(iteration 2); the entity gold set checks the extractor rules; intent_train trains the classifier (the QA set "
        "was checked against exact copies when it was written; near copies were not). A test question with a copy "
        "in these sets is therefore partly seen.",
        "",
        "## Exposed test items (failures viewed during development)",
        "",
        ", ".join(sorted(EXPOSED)),
        "",
        "Fixes for these were general rules backed by new dev cases with other names and wording; no test item was "
        "edited. But their patterns were seen, so their current pass rate is not an unbiased estimate.",
    ]
    run = latest_template_test_run()
    out += ["", "## Effect on the reported numbers (template mode, test split)", ""]
    if run is None:
        out.append("No template-mode test-split eval JSON found: run `just eval` first.")
    else:
        by_id = {q["id"]: q for q in run["questions"]}

        def acc(ids):
            got = [by_id[i] for i in ids if i in by_id]
            correct = sum(q["correct"] for q in got)
            return correct, len(got), sum(q.get("wrong_person", False) for q in got)

        groups = {
            "all test items": [r["id"] for r in test],
            "clean (not copied, not exposed)": clean,
            "copied in another gold set": sorted(copied),
            "exposed (failures viewed)": sorted(EXPOSED),
        }
        out += [f"From `reports/{run['_path']}` (template mode).", "",
                "| subset | n | QA accuracy | 95% lower bound (Clopper-Pearson) | wrong-person answers |",
                "|---|---|---|---|---|"]
        for name, ids in groups.items():
            c, n, wp = acc(ids)
            a = f"{100 * c / n:.1f}%" if n else "n/a"
            out.append(f"| {name} | {n} | {a} | {100 * lower_bound(c, n):.1f}% | {wp} |")
        out += [
            "",
            "Reading this:",
            "- The clean subset is the least biased estimate this set can give. Even so, at 100% its 95% lower bound is "
            "the honest figure to quote, not 100%.",
            "- Every subset was still written by the same author as the system, and the test split has **no** similar-name "
            "or ambiguous items (those exist only in dev). So none of these numbers measures wrong-person risk on unseen "
            "names; the dev split and the unit tests do, and they were tuned on.",
            "- Only a new, independently written test set (see reports/gold_review.md) can give an unbiased number.",
            "- All figures here are template mode. Live-LLM metrics are reported separately (`reports/llm_validation.md`, "
            "`reports/eval_*_llm.*`) and must not be merged with these.",
        ]
    return "\n".join(out) + "\n"


def main() -> None:
    REPORT.write_text(build_report(), encoding="utf-8")
    print(f"report: {REPORT.relative_to(PROJECT_ROOT).as_posix()}")


if __name__ == "__main__":
    main()
