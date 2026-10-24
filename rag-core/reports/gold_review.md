# Gold-set review

## Human review: NOT DONE (go-live condition, open)

No person has reviewed the gold sets yet. The review package is ready in `reports/human_review/`:
- `README.md`: instructions.
- `qa_review.csv`, `retrieval_review.csv`, `entities_review.csv`: every gold item with its facts written out and
  empty verdict columns.
- `system_answers.csv`: the system's answers. Kept separate, so the reviewer opens it only after the first pass.

Regenerate the package with `just review-pack` after any gold change.

The reviewer is asked to verify:
1. factual correctness against the faculty sheet;
2. acceptable paraphrases for each keypoint;
3. similar and ambiguous names (must never give another person's facts);
4. speech-to-text errors;
5. which questions must be handed over as unanswerable.

**Sign-off.** This condition is complete only when this table is filled in by the reviewer.

| field | value |
|---|---|
| reviewer name and role | |
| relationship to the project (must be independent of its authors) | |
| date | |
| files and rows reviewed | |
| items flagged (count, where recorded) | |
| verdict (accept / accept with changes / reject) | |
| signature or written confirmation | |

The AI pre-review below is input for the reviewer, **not** a substitute for this sign-off.

---

## AI pre-review (2026-10-24): not a human review

### Who reviewed, and the limits of it

The reviewer was a separate Claude agent with fresh context. It had no access to the authoring conversation and
was told not to read the answer templates or the LLM prompt, so it couldn't copy the system's wording. It had
read-only access to CLAUDE.md, `facts.jsonl`, the grading code (`eval/qa.py`) and the three gold sets. It changed
no files. It also wrote 30 new dev questions.

This reduces single-author bias, but it is **not an independent human review**. The reviewer is the same model
family as the author and may share its blind spots. A person who knows the department and its visitors should
still review the set, and ideally write a new frozen test set (see "Recommendations").

### Verdict

- **No structural errors.** Every `gold_fact_id` and entity id exists, there are no duplicate ids, and the factual
  keypoints (rooms, emails, times) match the KB.
- **The problems are in grading design, policy consistency and realism.** As written, the set rewards a system that
  copies the facts' exact wording.

### Findings and what was done

| finding (reviewer) | where | action |
|---|---|---|
| Keypoints copy the facts' wording ("not on this floor", raw class codes like "5 MCA A DEV LAB", "Ninth Floor", "R & D Block", "yes"), so a correct paraphrase fails | ~25 items, mostly test | **Grader:** a keypoint may now be a list of alternatives (any one counts). **Dev items** that were brittle were fixed (qa073, qa078). **Test items not changed** (frozen); see "Open" below. |
| qa078 "no class" never matches "no classes are listed" | dev | Keypoint now accepts either. |
| Policy contradictions: ambiguous names are answerable (clarify) while `ambiguous_entity` is an escalation code; similar names escalate in some items and clarify in qa180 | dev | **Policy made explicit, per the product owner's instruction:** when a person can't be confidently identified, ask instead of answering. A clarifying question that states no facts counts as a correct handling of an unanswerable question (`eval/qa.py`). `ambiguous_entity` stays for when no clarification can be offered. Title-case unknown names still decline and escalate, with "did you mean". |
| Gap facts handled inconsistently ("no classes listed" answered; "email not listed" escalates) | mixed | Rule documented in CLAUDE.md: a missing value the visitor needs to act on (email, hours, office) escalates; an empty timetable day is itself the answer. No gold change. |
| Wrong-person and ambiguous items exist only in dev; misspelt/ASR items only in test; the split is 35/65, not 30/70 | split | Not fixable without changing the frozen test split. **Open**: see recommendations. |
| `forbidden` strings forbid the value the visitor said ("900", "deepa@gmail.com", "Sharma"), so a rebuttal that repeats it fails | test qa163/164/167/168/174 | **Not changed** (frozen). Our system never repeats such values, so it isn't affected, but a better rebuttal style would be penalised. Kept as a known grading limit. |
| Missing-email items lack forbidden synthesised emails | qa045/046 test, qa047 dev | qa047 (dev): forbidden `suresh.k@christuniversity.in`. Test items open. |
| qa176 couldn't detect a wrong-person answer (Kavitha R also has no email); qa177's forbidden couldn't fire on a Tuesday | dev | qa176 is now a location question with forbidden "884". qa177 also forbids "877". |
| qa182 didn't require the name | dev | Keypoint "Tegil J John" added. |
| qa142 ("Dr John Mathew") labelled easy with no forbidden | test | Open (frozen). It passes: the answer declines and gives no room. |
| qa085 should be `schedule_query` (also in retrieval_test); intent labels elsewhere | test | Open (frozen). The answer passes either way. |
| **retrieval_test has verbatim/near copies of QA test questions** ("Does Dr Deepa V Jose teach OS?", "What is BSc?", "Where is the MSc AI and ML classroom?"…) and entities_test copies six QA questions | cross-set | **Disclosed.** The retrieval gold set was used to evaluate route changes, and one earlier intent fix (iteration 2) cited a question from it as evidence. The QA test split is therefore **not fully blind** for those questions. Open: de-duplicate. |
| entities_test: "where does SS sit" links Shoney Sebastian although "SS" are also initials of three others; "who teaches data structures" links only the topic | entities | Open (low). Short codes match only exactly as written, which mitigates the first. |
| Phrasing mirrors the KB: 57/184 questions use the sheet's trailing initials, no "sir/ma'am", little code-mixing, 6 noisy items | set | The reviewer's 30 questions (`rv001`–`rv030`, dev) add lowercase speech-to-text phrasing, "mam/sir/madam", Hinglish ("kahan hai", "kidhar hai"), spaced numbers, untested faculty (Rupali Sunil Wagh, Surabhi Saxena), unreadable hours with "now", overlapping class and consultation, and a TODO-code bait ("full stack development"). |

One edit to the reviewer's questions: rv027 "washroom kahan hai" duplicated an intent training seed, so it was
reworded to "toilet kidhar hai" (same intent and entity).

### What the reviewer's questions found

On first run, 8 of the 30 failed. None was a wrong-person answer or a hallucination:
- **rv010:** a class overlapping consultation hours was not surfaced.
- **rv014:** lowercase course code "rm" was not linked.
- **rv015:** "working on IoT" was answered as the course, not the research area.
- **rv016:** "image processing" missed "digital image processing".
- **rv017:** "dr neta" was declined instead of offering Neetha/Neha.
- **rv018:** "free now" with unreadable hours was answered instead of escalated.
- **rv019:** "where is the dbms lab" was answered with a list of teachers.
- **rv030:** "how many semesters is MCA" got a programme summary.

All 8 were fixed with general rules (see `reports/iterations.md`, iteration 7) and now pass.

## Recommendations (need a decision or a person)

1. **Have a person write a new frozen test set (v2)** with real or realistic kiosk questions:
   - lowercase speech-to-text phrasing, honorifics, Hinglish;
   - similar-name, ambiguous and noisy cases in **both** splits;
   - no copies across the qa, retrieval and entity sets.

   Keep the current 120-question test set for regression only.
2. **For v2, decide** whether `forbidden` may contain values the visitor said (the reviewer suggests not), and use
   alternatives for wording-sensitive keypoints.
3. **De-duplicate** retrieval_test and entities_test against the QA test split, and give retrieval_test ids.
4. **Report conversation items** (greetings, thanks) separately from QA accuracy: they always pass.
