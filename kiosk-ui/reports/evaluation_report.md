# Final evaluation report

Generated 2026-10-26T23:50:35 by `python -m kiosk_ui.evaluate` from `test_set.jsonl` (26 scenarios, 29 turns).
Every turn went through the kiosk (Portion 3) -> voice-memory (Portion 2) -> rag-core (Portion 1).

## Summary (end to end)

| measure | result |
|---|---|
| scenarios fully correct | 100.0% (26/26) |
| turns correct | 100.0% (29/29) |
| navigation: correct route shown | 100.0% (9/9) |
| navigation: no route where none should be | 100.0% (5/5) |
| escalations that should happen and did | 100.0% (4/4) |
| answerable questions wrongly escalated | 0.0% (0/21) |
| all escalations in the run | 5 |
| response time at the kiosk (typed) | mean 608.5 ms, p50 553.8 ms, p95 1009.4 ms |
| voice-memory turn total | mean 592.7 ms, p95 993.9 ms |

## By category

| category | turns | correct |
|---|---|---|
| clarification | 2 | 100.0% (2/2) |
| consultation | 3 | 100.0% (3/3) |
| contact | 1 | 100.0% (1/1) |
| escalation_no_data | 1 | 100.0% (1/1) |
| escalation_not_in_kb | 1 | 100.0% (1/1) |
| escalation_out_of_scope | 1 | 100.0% (1/1) |
| escalation_unknown_person | 1 | 100.0% (1/1) |
| follow_up | 4 | 100.0% (4/4) |
| location | 7 | 100.0% (7/7) |
| location_off_floor | 2 | 100.0% (2/2) |
| receptionist_request | 1 | 100.0% (1/1) |
| research | 1 | 100.0% (1/1) |
| room | 1 | 100.0% (1/1) |
| schedule | 1 | 100.0% (1/1) |
| social | 1 | 100.0% (1/1) |
| teaching | 1 | 100.0% (1/1) |

## Failed turns

None.

## Floor map

- Map: `floor_map.yaml`, 20 routable places, verified: **NO (placeholder layout)**.
- 8th-floor rooms in the faculty data: 14; missing from the map: none.
- Route lengths and left/right directions are only as good as the placeholder coordinates.

## Kiosk usage (from the analytics log)

266 kiosk events logged so far (includes this evaluation run).

## Numbers from the other modules

### rag-core (`entity_eval.md`)

#### Entity extraction + linking: evaluation

- Test set: `data/eval/entities_test.jsonl`, 105 questions (sha256 `34527a3441cc`).
- Fuzzy cut-off `thresholds.entity_match_min` = 85, ambiguity margin 3 points.
- Target: micro F1 >= 0.90 and every ambiguous name flagged: **PASS** (F1 1.000; ambiguous flagged 3/3, silently resolved 0).

##### Precision / recall / F1 by type

| type | precision | recall | F1 | gold | predicted |
|---|---|---|---|---|---|
| faculty | 1.00 | 1.00 | 1.00 | 55 | 55 |
| room | 1.00 | 1.00 | 1.00 | 9 | 9 |
| programme | 1.00 | 1.00 | 1.00 | 13 | 13 |
| course | 1.00 | 1.00 | 1.00 | 13 | 13 |
| topic | 1.00 | 1.00 | 1.00 | 15 | 15 |
| day | 1.00 | 1.00 | 1.00 | 10 | 10 |
| time | 1.00 | 1.00 | 1.00 | 14 | 14 |
| facility | 1.00 | 1.00 | 1.00 | 9 | 9 |
| event | 1.00 | 1.00 | 1.00 | 6 | 6 |
| **all (micro)** | **1.000** | **1.000** | **1.000** | 144 | 144 |

##### Errors: 0 questions

None.

Limits: the gold set and the rules were written by the same author, so this is optimistic. Label real kiosk questions before trusting it. Add new questions for failures instead of bending rules to individual test items.

### rag-core (`eval_integrity.md`)

#### Evaluation integrity: how blind is the frozen test split?

- Test split: 120 questions (byte-for-byte frozen; fingerprint checked by `tests/eval/test_qa_eval.py::test_test_split_is_frozen`).
- **17** test questions have a verbatim or near copy (token_sort_ratio >= 85, same numbers) in another gold set used during development; 4 more only share a sentence pattern with a different room/number (listed, not counted as copies).
- **16** test items had their failures viewed while fixes were developed.
- **88** test items are clean (neither copied nor exposed).

##### Similar questions in other gold sets

| test id | test question | found in | as | similarity | kind |
|---|---|---|---|---|---|
| qa013 | where is Dr Thirunavukkarasu V | retrieval_test | Where is Dr Thirunavukkarasu's room | 86 | near copy |
| qa013 | where is Dr Thirunavukkarasu V | entities_test | who is Dr Thirunavukarasu | 87 | near copy |
| qa013 | where is Dr Thirunavukkarasu V | intent_test (held-out) | Who is Dr Thirunavukkarasu? | 89 | near copy |
| qa025 | where is Dr Ramamurthy B | retrieval_test | who is Dr Ramamurthy B | 91 | near copy |
| qa030 | where is Dr Suresh K's cabin | retrieval_test | where is Dr Suresh K's cabin | 100 | verbatim |
| qa030 | where is Dr Suresh K's cabin | entities_test | Where is Suresh sir's cabin? | 87 | near copy |
| qa058 | Can I meet Dr Chandra J right now? | retrieval_test | When can I meet Dr Chandra J? | 85 | near copy |
| qa079 | When does Dr Deepa V Jose teach OS? | retrieval_test | Does Dr Deepa V Jose teach OS? | 92 | near copy |
| qa082 | Who teaches operating systems here? | retrieval_test | who teaches operating systems | 92 | near copy |
| qa082 | Who teaches operating systems here? | entities_test | who teaches operating systems | 92 | near copy |
| qa082 | Who teaches operating systems here? | intent_train | Who teaches operating systems? | 92 | near copy |
| qa085 | Does Dr Deepa V Jose teach OS? | retrieval_test | Does Dr Deepa V Jose teach OS? | 100 | verbatim |
| qa091 | who teaches machine learning | entities_test | Who teaches machine learning? | 100 | verbatim |
| qa091 | who teaches machine learning | intent_train | Who teaches machine learning? | 100 | verbatim |
| qa091 | who teaches machine learning | intent_train | machine lerning who teaches | 98 | near copy |
| qa093 | Who works on blockchain? | retrieval_test | who works on blockchain | 100 | verbatim |
| qa095 | Who is the expert on computer vision? | retrieval_test | who is the expert in computer vision | 91 | near copy |
| qa096 | who does natural language processing research | retrieval_test | Who does research in natural language processing? | 96 | near copy |
| qa101 | Who sits in room 872? | retrieval_test | who sits in room 871 | 95 | same pattern |
| qa102 | How do I get to room 883? | retrieval_test | How do I get to room 880 | 95 | same pattern |
| qa102 | How do I get to room 883? | intent_train | How do I get to room 877? | 91 | same pattern |
| qa106 | Where is room 607A? | retrieval_test | Where is room 874? | 85 | same pattern |
| qa106 | Where is room 607A? | retrieval_test | where is room 974 | 85 | same pattern |
| qa106 | Where is room 607A? | retrieval_test | where is room 710 | 85 | same pattern |
| qa106 | Where is room 607A? | retrieval_test | where is room 014 | 85 | same pattern |
| qa106 | Where is room 607A? | entities_test | Where is room 874? | 85 | same pattern |
| qa106 | Where is room 607A? | entities_test | where is staff room 607A | 85 | near copy |
| qa106 | Where is room 607A? | intent_train | Where is room 874? | 85 | same pattern |
| qa106 | Where is room 607A? | intent_test (held-out) | Where is room 879? | 85 | same pattern |
| qa106 | Where is room 607A? | intent_test (held-out) | where is staff room 607A | 85 | near copy |
| qa107 | who is in room 879 | retrieval_test | who is in room 885 | 88 | same pattern |
| qa108 | Where is room 900? | retrieval_test | where is room 974 | 88 | same pattern |
| qa108 | Where is room 900? | retrieval_test | where is room 710 | 88 | same pattern |
| qa108 | Where is room 900? | retrieval_test | where is room 014 | 88 | same pattern |
| qa108 | Where is room 900? | entities_test | Where is room 999? | 88 | same pattern |
| qa108 | Where is room 900? | intent_test (held-out) | Where is room 879? | 88 | same pattern |
| qa114 | What subjects are in the MSc AI and ML timetable? | intent_train | What subjects are taught in MSc AI and ML? | 85 | near copy |
| qa115 | what is BSc | retrieval_test | What is BSc? | 100 | verbatim |
| qa117 | What is the subject DAA? | retrieval_test | what is the subject DAA about | 88 | near copy |
| qa148 | When will the semester results come out? | intent_test (held-out) | when will the semester exam results come | 91 | near copy |
| qa151 | thank you so much for the help | intent_train | thank you very much for the help | 87 | near copy |

How they were used: the retrieval gold set drove route changes and served as evidence for an intent-data fix (iteration 2); the entity gold set checks the extractor rules; intent_train trains the classifier (the QA set was checked against exact copies when it was written; near copies were not). A test question with a copy in these sets is therefore partly seen.

##### Exposed test items (failures viewed during development)

qa017, qa024, qa035, qa079, qa086, qa090, qa107, qa118, qa120, qa123, qa142, qa143, qa146, qa159, qa170, qa174

Fixes for these were general rules backed by new dev cases with other names and wording; no test item was edited. But their patterns were seen, so their current pass rate is not an unbiased estimate.

##### Effect on the reported numbers (template mode, test split)

From `reports/eval_20261024_085117.json` (template mode).

| subset | n | QA accuracy | 95% lower bound (Clopper-Pearson) | wrong-person answers |
|---|---|---|---|---|
| all test items | 120 | 100.0% | 97.0% | 0 |
| clean (not copied, not exposed) | 88 | 100.0% | 95.9% | 0 |
| copied in another gold set | 17 | 100.0% | 80.5% | 0 |
| exposed (failures viewed) | 16 | 100.0% | 79.4% | 0 |

Reading this:
- The clean subset is the least biased estimate this set can give. Even so, at 100% its 95% lower bound is the honest figure to quote, not 100%.
- Every subset was still written by the same author as the system, and the test split has **no** similar-name or ambiguous items (those exist only in dev). So none of these numbers measures wrong-person risk on unseen names; the dev split and the unit tests do, and they were tuned on.
- Only a new, independently written test set (see reports/gold_review.md) can give an unbiased number.
- All figures here are template mode. Live-LLM metrics are reported separately (`reports/llm_validation.md`, `reports/eval_*_llm.*`) and must not be merged with these.

### rag-core (`gold_review.md`)

#### Gold-set review

##### Human review: NOT DONE (go-live condition, open)

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

##### AI pre-review (2026-10-24): not a human review

###### Who reviewed, and the limits of it

The reviewer was a separate Claude agent with fresh context. It had no access to the authoring conversation and
was told not to read the answer templates or the LLM prompt, so it couldn't copy the system's wording. It had
read-only access to CLAUDE.md, `facts.jsonl`, the grading code (`eval/qa.py`) and the three gold sets. It changed
no files. It also wrote 30 new dev questions.

This reduces single-author bias, but it is **not an independent human review**. The reviewer is the same model
family as the author and may share its blind spots. A person who knows the department and its visitors should
still review the set, and ideally write a new frozen test set (see "Recommendations").

###### Verdict

- **No structural errors.** Every `gold_fact_id` and entity id exists, there are no duplicate ids, and the factual
  keypoints (rooms, emails, times) match the KB.
- **The problems are in grading design, policy consistency and realism.** As written, the set rewards a system that
  copies the facts' exact wording.

###### Findings and what was done

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

###### What the reviewer's questions found

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

##### Recommendations (need a decision or a person)

1. **Have a person write a new frozen test set (v2)** with real or realistic kiosk questions:
   - lowercase speech-to-text phrasing, honorifics, Hinglish;
   - similar-name, ambiguous and noisy cases in **both** splits;
   - no copies across the qa, retrieval and entity sets.

   Keep the current 120-question test set for regression only.
2. **For v2, decide** whether `forbidden` may contain values the visitor said (the reviewer suggests not), and use
   alternatives for wording-sensitive keypoints.
3. **De-duplicate** retrieval_test and entities_test against the QA test split, and give retrieval_test ids.
4. **Report conversation items** (greetings, thanks) separately from QA accuracy: they always pass.

### rag-core (`intent_eval.md`)

#### Intent classifier: held-out evaluation

- Test set: `data/eval/intent_test.jsonl`, 130 examples (sha256 `b182bc696f2d`), never used for training or tuning.
- Model: `data/models/intent.joblib`, logistic regression C=1000, trained on 497 examples (train CV accuracy 0.903).
- Embeddings: `BAAI/bge-small-en-v1.5` (query prefix as in retrieval).
- Target: accuracy >= 0.90 without the LLM fallback: **PASS** (0.985).

##### Summary

| setting | accuracy | macro F1 | decided by rule / model / LLM | below fallback threshold | LLM calls |
|---|---|---|---|---|---|
| without LLM fallback | 0.985 | 0.985 | 22 / 108 / 0 | 0 | 0 |

LLM fallback is disabled (`intent.llm_fallback_enabled: false`), so only the setting without it was evaluated.

##### Per-intent precision / recall (without LLM fallback)

| intent | precision | recall | F1 | support |
|---|---|---|---|---|
| find_person | 1.00 | 1.00 | 1.00 | 10 |
| person_availability | 1.00 | 1.00 | 1.00 | 10 |
| find_by_subject | 0.91 | 1.00 | 0.95 | 10 |
| find_room | 1.00 | 1.00 | 1.00 | 10 |
| schedule_query | 1.00 | 1.00 | 1.00 | 10 |
| programme_info | 0.90 | 0.90 | 0.90 | 10 |
| event_info | 1.00 | 1.00 | 1.00 | 10 |
| facility_info | 1.00 | 1.00 | 1.00 | 10 |
| contact_info | 1.00 | 1.00 | 1.00 | 10 |
| greeting | 1.00 | 1.00 | 1.00 | 10 |
| smalltalk_or_thanks | 1.00 | 1.00 | 1.00 | 10 |
| escalate_to_human | 1.00 | 1.00 | 1.00 | 10 |
| out_of_scope | 1.00 | 0.90 | 0.95 | 10 |

##### Confusion matrix (without LLM fallback)

Rows are the gold intent, columns the predicted intent (numbered as the rows).

| gold \ predicted | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1. find_person | **10** | · | · | · | · | · | · | · | · | · | · | · | · |
| 2. person_availability | · | **10** | · | · | · | · | · | · | · | · | · | · | · |
| 3. find_by_subject | · | · | **10** | · | · | · | · | · | · | · | · | · | · |
| 4. find_room | · | · | · | **10** | · | · | · | · | · | · | · | · | · |
| 5. schedule_query | · | · | · | · | **10** | · | · | · | · | · | · | · | · |
| 6. programme_info | · | · | 1 | · | · | **9** | · | · | · | · | · | · | · |
| 7. event_info | · | · | · | · | · | · | **10** | · | · | · | · | · | · |
| 8. facility_info | · | · | · | · | · | · | · | **10** | · | · | · | · | · |
| 9. contact_info | · | · | · | · | · | · | · | · | **10** | · | · | · | · |
| 10. greeting | · | · | · | · | · | · | · | · | · | **10** | · | · | · |
| 11. smalltalk_or_thanks | · | · | · | · | · | · | · | · | · | · | **10** | · | · |
| 12. escalate_to_human | · | · | · | · | · | · | · | · | · | · | · | **10** | · |
| 13. out_of_scope | · | · | · | · | · | 1 | · | · | · | · | · | · | **9** |

##### Misclassified (without LLM fallback): 2

| question | gold | predicted | probability | decided by |
|---|---|---|---|---|
| Which faculty teach in the MSc AIML programme? | programme_info | find_by_subject | 0.76 | model |
| fee structure for MCA course | out_of_scope | programme_info | 0.60 | model |

Do not copy these questions into the training set. That would leak the test set. Add new, different examples of the same kind instead, then retrain and re-run.

### rag-core (`iterations.md`)

#### Failure analysis and fix iterations

Generation in templates mode (no LLM), so every number is deterministic. Each iteration made one change and re-ran
`just eval` on the dev split (used for decisions) and the test split (reported, never used to choose a fix).

**Rule used to stay off the test split:** a fix was implemented only when its root cause also showed up on the dev
split or in runs made before the QA eval existed (the generation smoke run, `data/eval/retrieval_test.jsonl`).
Causes seen only on test are listed under "Proposed, not implemented" with the smallest fix, for you to approve.
New training examples avoid the names and wording of QA questions. The held-out intent set was only re-measured, never used to choose.

##### Root causes of the baseline failures (iteration 0)

10 test failures + 3 dev failures, grouped by the first cause in the chain. There were no retrieval misses that
weren't caused by an intent error (with the gold intent, Recall@5 is 1.000 in `reports/retrieval_eval.md`), no
grounding false positives (0 `grounding_failed`), and no over-strict threshold (0 `low_confidence`; the sweep in
`reports/threshold_sweep.md` is flat).

| root cause | count (test + dev) | two examples | smallest fix |
|---|---|---|---|
| **Intent error** | 5 + 2 | qa086 "What subjects does Dr Gobi R teach?" -> find_person (profile instead of subjects); dev qa116 "What is TML?" -> out_of_scope | Training examples for the uncovered patterns ("what does Dr X teach" without a day; "what is <code>"). |
| **Generation ignored facts / answered something not asked** | 3 + 2 | qa024 "Is Dr Rajesh Kanna R on this floor?" -> "...is not on this floor." (room dropped); qa123 "Where are the BCA classes held?" -> a programme summary | Keep the full location sentence in profile answers; programme and person templates must not answer questions about classroom locations, fees or personal details. |
| **Entity linking miss** | 2 + 0 | qa142 "Where is Dr John Mathew sitting?" -> linked to Dr Tegil J John via the surname alias "John" (wrong person); qa174 "Who teaches ML? I think it's Dr Sharma." -> the unknown "Dr Sharma" overrode the ML answer | Unlink a titled name whose other words aren't in the KB; let the unknown-name answer apply only when nothing else in the question can be answered. |
| **Multi-entity** | 1 + 0 | qa159 "Where are Dr Nisha Varghese and Dr Deepa V Jose?" -> only the first | The location template answers each linked person (up to 3). |
| **Retrieval miss** | 0 independent | (qa079, qa086, qa090 missed gold facts only because the predicted intent filtered their category out) | none: fixed by the intent fixes |
| **Grounding false positive** | 0 | | none |
| **Threshold too strict** | 0 | | none |
| **Data gap in KB** | 0 failures, but 33 of 120 test questions escalate `not_in_kb` | "Where is the MSc AI and ML classroom?"; "What is Dr Kavitha R's email?" | Collect the data: `reports/data_requests.md` |
| **Unverified glossary** | 0 failures | every course answer says the code ("ML is taught by...") because no glossary entry is verified; 30 codes are TODO | Department verification: `reports/data_requests.md` §2 |

##### Iterations

Metrics in %. QA = QA accuracy; UA = unanswerable questions answered confidently (lower is better); H = hallucination
rate of answered questions; Cov = coverage. Δ = change in test QA vs the previous row.

| # | change | evidence (why) | dev intent | dev R@5 | dev QA | dev UA | test intent | test R@5 | test QA | test H | test UA | test Cov | Δ test QA |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | baseline | | 88.9 | 97.3 | 94.4 | 11.8 | 92.5 | 96.9 | 91.7 | 0.0 | 8.6 | 72.5 | |
| 1 | Profile answers keep the whole location statement (off-floor rooms were cut off) | dev qa067 answer ended "...is not on this floor." | 88.9 | 97.3 | 94.4 | 11.8 | 92.5 | 96.9 | 93.3 | 0.0 | 8.6 | 72.5 | +1.6 |
| 2 | 8 intent examples: "what does / which subjects does Dr X teach" -> schedule_query (retrain picked C=50) | smoke run: "what does Dr Gobi R teach" -> find_person | 88.9 | 98.6 | 94.4 | 5.9 | 92.5 | 97.9 | 92.5 | 0.0 | 11.4 | 71.7 | -0.8 |
| 2b | One new example used "Dr Sudhakar T", who then read as schedule_query in an availability question: swapped for a name not in the QA set (retrain picked C=500) | dev qa056 broke in #2 | 88.9 | 98.7 | 96.3 | 5.9 | 92.5 | 98.0 | 93.3 | 0.0 | 8.6 | 71.7 | +0.8 |
| 3 | 5 intent examples: "is Dr X available on <day>" -> person_availability (retrain picked C=1000) | dev qa056 still schedule_query | 88.9 | 100.0 | 96.3 | 11.8 | 91.7 | 99.0 | 92.5 | 0.0 | 11.4 | 72.5 | -0.8 |
| 4 | Programme template: "where are the classes" -> the "classroom locations not listed" gap; fees/admissions/hostel/... -> decline | dev qa121 flipped to programme_info in #3 and got a programme summary | 88.9 | 100.0 | 98.1 | 5.9 | 91.7 | 99.0 | 94.2 | 0.0 | 5.7 | 70.8 | +1.7 |
| 5 | Person templates decline personal-details questions (home address, salary, age, family) | dev qa166 "...home address" got a profile answer | 88.9 | 100.0 | 100.0 | 0.0 | 91.7 | 99.0 | 95.0 | 0.0 | 2.9 | 70.0 | +0.8 |

Net on test: QA 91.7 -> 95.0 (+3.3 pts), unanswerable answered 8.6 -> 2.9, Recall@5 96.9 -> 99.0, hallucination
0.0 throughout. Intent accuracy dropped 92.5 -> 91.7. Coverage fell 72.5 -> 70.0 because three wrong answers now
escalate correctly.

Notes, read before trusting the table:
- **Retraining is noisy.** Every retrain re-picks C by cross-validation (50, 500, 1000), and that flips a few borderline
  questions unrelated to the change (#2 and #3: test qa017 "dr shivangi singh office please", qa035 "who is Dr Jossy P
  George", qa143 fees). Single-question moves after a retrain are mostly that noise. The template guards in #4/#5
  make the outcome robust to some of these misroutes.
- **Two test fixes came from rules, not dev items.** The fees cue in #4 (from the CLAUDE.md out-of-scope list) also
  fixed test qa123/qa143, and "salary" in #5 fixed test qa146. The motivating dev items were qa121 and qa166.
- Held-out intent accuracy (`reports/intent_eval.md`): 0.992 before, 0.985 after (only re-measured). Train CV 0.90.
- Dev is now 100% correct, so it can't drive further fixes. Add newly labelled questions (ideally real kiosk logs) to dev.

##### Iteration 6: the wrong-person bug ("Dr John Mathew" -> Dr Tegil J John), 2026-10-24

###### Root cause

Every faculty member's first and last name are single-word aliases in the gazetteer (`AliasSource.NAME_PART`),
so "John" alone links Dr Tegil J John. In "Dr John Mathew" the span matcher linked "John" (an exact alias)
and nothing looked at the next word: "Mathew" fits no faculty name, so it was simply left over, and the
"Dr <unknown name>" rule never fired because the word after "Dr" was already taken. The retriever then pulled
Dr Tegil J John's facts by id, and the answer was fully grounded in the KB, so the grounding check had nothing
to object to. **It was a correct answer about the wrong person.**

Measured before the fix, the bug was broader than the one reported case. These all linked a real faculty member
the visitor never named: "Dr John Mathew" (also lowercase, without the title, and "Dr John K Mathew"),
"Dr Mathew John", "Dr Nisha Sharma", "Dr Kavitha Menon", "Dr Deepa Nair", "dr sridevi krishnan". "Dr George
Kurian" became a question about the two real Georges.

###### Development cases added first (dev split and unit tests; the 120 test rows are byte-identical)

- `data/eval/qa_test.jsonl`, 10 new **dev** questions (qa175-qa184), with names other than "John Mathew":
  - **Incorrect person resolution** (must escalate, and `forbidden` holds the near miss's room or hours): "Dr Nisha
    Sharma", "Dr Kavitha Menon", "Dr Deepa Nair", "Dr George Kurian", "Dr Mathew John".
  - **Lowercase:** "dr sridevi krishnan" must ask for confirmation and must not give room 875.
  - **Exact-name controls:** "Dr Tegil J John", "Dr John".
  - **Similar name:** "Dr Kavitha R" (vs Dr Rajesh Kanna R).
  - **Ambiguous:** "Dr George" (a clarification).
  - New difficulty value `similar_name`.
- `data/eval/entities_test.jsonl`: 6 cases (4 unknown surnames, "Dr John", "Kindly Dr Nisha").
- `tests/nlu/test_name_grounding.py`: 21 names that must resolve (exact, misspelt, honorifics, "Nisha moved to
  room 900", "Dr Somnath CS department"), 2 ambiguous, 12 that must not resolve, and a scan of all 526 fact
  texts. That scan caught a false positive during development ("Dr Thirunavukkarasu V. However" read as a longer
  name), which made test qa107 fail until it was fixed.
- `tests/scoring/test_grounding.py` and `tests/test_pipeline.py`: the decline with "did you mean", the lowercase
  confirmation, and **injected fake-LLM answers** about the near miss (with a valid citation) and about another
  retrieved person. Both must be blocked as `grounding_failed`.
- `tests/eval/test_qa_eval.py`: a zero-tolerance gate, with no wrong-person answer allowed on dev or test.
- Eval: a new **wrong-person** metric judged against the gold entities (not the system's own checker).

###### Implementation

1. **Extractor, name completeness** (`EntityExtractor._reject_partial_names`). After span matching, a faculty
   match with a name-like word beside it that fits no word of the candidate's name is replaced by an
   **unlinked** mention (`method="partial_name"`, contract `canonical_id: null`). The candidate is kept only in
   `near_misses`.
   - **Name-like** means a Title-case word in mixed-case text. Case gives no signal in all-lowercase text, so
     there it must be a word right after "dr"/"prof"/"fr", and the result asks for confirmation
     (`needs_confirmation`).
   - Stopwords, honorifics ("Sir", "Ma'am"), words claimed by other entities, sentence-initial capitals before the
     name and words before a title ("Kindly Dr Nisha") never count.
   - A misspelt surname that fits the candidate ("Vargese") keeps the link. Initials are skipped unless they are
     the candidate's own.
2. **No facts for a near miss.** Retrieval gets no linked id, so nothing is pulled by id. The template answer is
   "I'm sorry, I can't find anyone called Dr John Mathew in the faculty list. Did you mean Dr Tegil J John?
   Please check with the receptionist at the desk." It escalates as `not_in_kb`. In lowercase it is "Just to make
   sure I have the right person: do you mean Dr Sridevi R?", a clarification, not escalated.
3. **Defence in depth for the LLM path.** A new grounding issue `wrong_person` covers person intents. It fires
   when the question links person X and the answer names faculty but not X, or when the question names an
   unknown person (and nothing else) and the answer names anyone. It escalates as the existing
   `grounding_failed`. The prompt marks a near miss as "a DIFFERENT person" and has a new rule 11
   (`docs/prompts.md` regenerated).
4. `escalation.decide` accepts a clarification with one option (a confirmation). AMBIGUOUS_ENTITY still applies
   when no option can be offered.

Unchanged: the weighted confidence formula, every escalation code, the `RAGResult` contract (no
`clarification_options`), and the escalation threshold (the dev sweep still picks 0.45).

###### Results

All 341 tests pass (none skipped), including the model-backed ones. Entity F1 is 1.000 with ambiguous names
3/3 flagged. Retrieval with the gold intent: Recall@5 1.000, unchanged. The documented API examples still
reproduce. `just eval` shows no regression against the baseline (exit 0).

Before = the same code with the new pass switched off (the old extractor), run back-to-back with "after".

| metric | dev before | dev after | test before (= old baseline) | test after |
|---|---|---|---|---|
| **wrong-person answers** | **4** (qa175, qa177, qa179, qa180) | **0** | **1** (qa142) | **0** |
| QA accuracy | 92.2% | 100.0% | 95.0% | 95.8% |
| hallucination rate (answered) | 8.7% (the wrong-person answers broke `forbidden`) | 0.0% | 0.0% | 0.0% |
| unanswerable answered confidently | 18.2% | 0.0% | 2.9% | 0.0% |
| retrieval Recall@5 | 100.0% | 100.0% | 99.0% | 99.0% |
| intent accuracy | 89.1% | 89.1% | 91.7% | 91.7% |
| escalation precision / recall | 100% / 81.8% | 100% / 100% | 94.4% / 97.1% | 94.6% / 100% |
| coverage (answered) | 71.9% | 65.6% | 70.0% | 69.2% |
| latency total p50 / p95 (ms) | 51.6 / 62.6 | 50.5 / 60.0 | 52.6 / 59.6 | 52.2 / 61.1 |

- **Latency.** The new pass costs about 0.03 ms per question at p50 and under 0.1 ms at p95 (`extract()`
  micro-benchmark over all 184 questions, 5 runs). The p95 differences in the table are run-to-run noise. All
  totals were about 3x higher today than on 2026-10-23 (p95 about 19 ms then), on both the old and the new
  code: the machine was slower, and the embedding step dominates.
- **Coverage** fell because wrong answers now escalate correctly: qa142 on test; qa175, qa177, qa178, qa179 on dev.
- **qa176** ("Dr Kavitha Menon's email") counted as correct before only by luck: the old system answered about
  Dr Kavitha R, whose email happens to be unlisted, so it escalated anyway.
- **`reports/baseline.json` refreshed** to the "test after" column. Old values: intent 0.925*, Recall@5 0.969*,
  QA 0.917*, hallucination 0.0, unanswerable answered 0.086*. *These are from 2026-10-23 before iterations 1-5;
  the baseline was already refreshed once after iteration 5 (QA 0.950, unanswerable 0.029, Recall@5 0.990,
  intent 0.917), and this refresh replaces that.

###### Known trade-offs and limits of this fix

- In **all-lowercase** text a non-name word straight after "dr <first name>" triggers a confirmation question:
  "where is dr nisha moved" -> "do you mean Dr Nisha Varghese?". This costs one extra turn but gives no wrong
  information. Mixed-case text ("I heard Dr Nisha moved to room 900") is unaffected.
- All-lowercase text **without a title** ("where is john mathew") still links Dr Tegil J John. Case gives no
  signal, and "john" alone is a valid alias. If the STT output is lowercase, add a title, enable casing in STT,
  or accept this gap. The LLM path's `wrong_person` check doesn't catch it either, because the question then
  links Dr Tegil J John.
- In Title-Case Text, an ordinary word that isn't on the stopword list reads as a surname: "Is Dr John Inside?",
  "Has Dr Nisha Left?" and "Dr Gobi Upstairs?" get "I can't find anyone called ... Did you mean ...?" (verified).
  This is safe, but costs one more turn. Common words ("Available", "Free", "Office", "Cabin") are stopwords
  and keep the link.
- Every test and eval number here comes from templates mode. The LLM path's protection (prompt rule 11 +
  `wrong_person` check) is verified only with fake LLM answers.

###### Remaining test-split failures (5 of 120, unchanged by this fix, still proposals below)

qa017 ("dr shivangi singh office please" -> consultation hours), qa035 ("who is Dr Jossy P George" -> safe
message), qa079 ("When does Dr Deepa V Jose teach OS?"), qa159 (two people in one question: only the first is
answered), qa174 ("Who teaches ML? I think it's Dr Sharma." -> declines because of "Dr Sharma"). None of them gives
wrong information (0 hallucinations, 0 wrong-person). They are missing or over-cautious answers.

**Not production-ready yet.** The wrong-person bug is resolved and verified in templates mode, on dev, test and
the unit tests. Still open before a go-live:
- No live LLM has been run at all.
- The lowercase-without-title gap above.
- The 5 failures above.
- All gold sets were written by the same author: label real kiosk questions before trusting these numbers.

##### Iteration 7: production-readiness pass (2026-10-24)

**Scope:**
- Lowercase person names.
- Fewer needless clarifications.
- The five remaining test failures.
- An independent gold-set review and its new dev questions.
- The live LLM path.

**Unchanged:** the API contract, the confidence formula, the threshold (0.45; the dev sweep still recommends 0.45),
the escalation codes, and the 120-question test split (its fingerprint is now enforced by
`tests/eval/test_qa_eval.py::test_test_split_is_frozen`). Every new case went to **dev**: 18 regression cases
(qa185–qa202) and the reviewer's 30 (rv001–rv030). Dev now has 112 questions.

###### 7a. Lowercase names and needless clarifications

- **Root cause.** Iteration 6 only treated a word beside a partly matched name as "more name" if it was Title-case,
  or followed a title in lowercase text.
  - "where is john mathew" (speech-to-text without capitals or a title) still linked Dr Tegil J John.
  - Conversely, any capitalised ordinary word ("Is Dr John Inside?", "Has Dr Nisha Left?") was read as a surname.
- **Fix (`nlu/entities.py`, `data/kb/entity_lexicon.yaml`):**
  - **New lexicon list.** `name_adjacent_words` holds words said beside a name that are never part of one: inside,
    left, gone, back, upstairs, moved, call, visit… A test checks that no listed word is part of any faculty name.
  - **Lowercase words count.** Any other lowercase word beside a partial match counts, with or without a title, and
    in all-lowercase or mixed text. The answer is then a confirmation question ("do you mean Dr Tegil J John?") with
    no facts.
  - **Title-case words still decline.** A Title-case word still gives the decline plus "did you mean", escalated
    `not_in_kb`.
  - **Full names are exempt.** The check only applies to partial matches. Initials count as part of the name:
    "Dr Sridevi" is partial, "Dr Deepa V Jose" is full.
  - **Before the name,** a lowercase word only counts in all-lowercase text. Our own clarification sentence
    ("…matches George: …") had failed grounding during development until this was fixed.
  - **Titled unknown names** ("dr neta") now get near-miss suggestions by fuzzy name similarity: a clarification in
    lowercase, a decline with "did you mean" in Title-case. Nobody is ever linked.
- **Verified:**
  - "where is john mathew" / "mathew john" / "Dr John mathew" / "dr sridevi krishnan" ask for confirmation.
  - "Is Dr John Inside?", "Has Dr Nisha Left?", "Dr Gobi Upstairs?", "Can Dr Deepa help me?" and "dr nisha moved" link
    normally.
  - All 526 fact texts are still free of partial-name hits.

###### 7b. The five remaining test failures

They were named by you from the test split. Each fix is a general rule backed by **new dev cases with other names
and wording**, never by editing or re-reading the test items' wording. Because the five were identified on test,
the test split is now **less blind for these patterns**: read test 100% as optimistic.

| test failure | root cause | general fix | dev cases |
|---|---|---|---|
| qa017 "dr shivangi singh office please" -> consultation hours | Intent read "office" as office hours; the availability template answered a question nobody asked | Availability and contact templates answer **where** when the question asks where and has no availability or contact words ("office hours" still gets hours). Availability and contact intents also fetch the office (low-tier route). | "sudhakar sir office please", "dr gobi r room please", counter-case "dr gobi office hours please" |
| qa035 "who is Dr Jossy P George" -> safe message | Misrouted to find_by_subject, which had no answer for "only a person linked" | find_by_subject with no subject linked answers about the person (profile) | "who is dr chandra j", "tell me who Dr Helen K Joy is" |
| qa079 "When does Dr Deepa V Jose teach OS?" -> "Yes, teaches OS" (no days) | find_by_subject didn't fetch the schedule | New route: find_by_subject with person + course also fetches that course's schedule days. The template gives the days ("teaches OS on Monday, Tuesday and Friday"); yes/no questions start with "Yes" | "On which days does Dr Neha Singhal take SE?", "when does dr somnath sinha teach ndm", "Does Dr Sridevi R teach DSA?" |
| qa159 "Where are Dr Nisha Varghese and Dr Deepa V Jose?" -> only the first | Location template used only the first person | Location questions with several people answer each one (one sentence each, up to 3, conflicts noted) | "Where are Dr Gobi R and Dr Cynthia T?", "where do dr sridevi r and dr ashwini patil sit" |
| qa174 "Who teaches ML? I think it's Dr Sharma." -> "can't find Dr Sharma" | The unknown-name decline overrode the answerable course part | With another KB entity linked, answer that part. If the name partly matched someone, add "The other name you mentioned isn't in the faculty list." (it doesn't repeat the name, so it can't sound like a confirmation) | "Who teaches DSA? Someone told me it was Dr Verma.", "Does Dr Iyer teach machine learning?", "Does Dr Nisha Sharma teach ML?" |

Also found by these dev cases and fixed:
- **Presence questions** ("Is Dr Cynthia Inside?") classified as find_person now answer availability, unless a room
  is named ("Is Dr Sridevi in room 877?" stays a location question). find_person also fetches consultation hours as a
  low-tier route.

###### 7c. Independent gold review (see `reports/gold_review.md`)

- **Reviewer:** a fresh-context agent, not a human.
- **Applied:**
  - Keypoints can list alternatives.
  - A clarifying question with no facts counts as correct for an unanswerable question (the policy you set).
  - Six dev items fixed.
  - Its 30 new dev questions added. 8 failed at first; all are fixed with general rules:
    - an overlapping class and consultation are both stated (rv010);
    - a lowercase course code is accepted right after "teaches" (rv014);
    - research wording prefers the research topic (rv015);
    - topics include the longer research areas that contain them (rv016);
    - unknown names get "did you mean" (rv017);
    - "free now" with unreadable hours escalates as unknown, per CLAUDE.md (rv018);
    - "where is the <course> lab" gets the "not listed" answer (rv019);
    - duration and semester questions decline (rv030).
- **Not applied** (frozen test split): test-item label, keypoint and forbidden issues. They are listed in the
  review, with a recommendation for a new human-written test set.
- **Disclosed:** retrieval_test contains copies of QA test questions and was used as evidence earlier, so the test
  split is not fully blind.

###### 7d. LLM path

- **Installed** the `anthropic` extra (SDK 2.3.0) in the project venv.
- **Credential handling:** keys are read only from `.env` (git-ignored) and held as `SecretStr`. A test checks that
  the key appears in no result, query-log line, health status or settings repr.
- **Fixes found while validating:**
  - With the SDK installed but no credentials, the client built fine and failed on the first call. It now reports
    "no Anthropic credentials" at start-up (`/health`), and the kiosk answers from templates.
  - Any unexpected SDK exception now falls back to templates instead of failing the request.
- **Validated against the real SDK with a mock HTTP transport** (`tests/generation/test_anthropic_adapter.py`,
  18 tests):
  - **Request:** exact request shape (model, structured output, effort, refusal fallbacks, no temperature).
  - **Malformed output:** retried once, then fails.
  - **Refusal and max_tokens:** both raise an error.
  - **Timeout:** retried once, then fails.
  - **HTTP errors:** 5xx recovers on retry; 401 isn't retried.
  - **Fallback:** every failure falls back to templates in the pipeline.
  - **Wrong person:** an LLM wrong-person answer is blocked as `grounding_failed`.
  - **Near misses:** a near miss reaches the model marked "a DIFFERENT person".
- **Live network validation:** `just validate-llm` (`rag_core.generation.validate_llm`) and an opt-in
  `pytest -m live` test are written. **It has not run: there are no credentials on this machine** (no
  `ANTHROPIC_API_KEY`, no `ant` CLI, no Ollama). `reports/llm_validation.md` says "NOT RUN".

###### Results

All 361 tests: 359 pass and 2 are skipped (the opt-in live test, and a legacy test that only applies without the SDK).
Retrieval with the gold intent: Recall@5 1.000. Entity F1 1.000. `just eval` shows no regression (exit 0).

**Test split (120 frozen questions).** The original baseline is iteration 0 (2026-10-23).

| metric | original baseline | iteration 6 | **now** |
|---|---|---|---|
| intent accuracy | 92.5% | 91.7% | 91.7% |
| retrieval Recall@5 | 96.9% | 99.0% | **100.0%** |
| QA accuracy | 91.7% | 95.8% | **100.0%** |
| hallucination rate | 0.0% | 0.0% | 0.0% |
| unanswerable answered confidently | 8.6% | 0.0% | 0.0% |
| wrong-person answers | 1 (not measured then; qa142) | 0 | 0 |
| escalation precision / recall | 97.0% / 91.4%* | 94.6% / 100% | 100% / 100% |
| coverage | 72.5% | 69.2% | 70.8% |
| latency p50 / p95 | 15.1 / 19.6 ms* | 52 / 61 ms** | 14.6 / 18.2 ms |

\* From `reports/eval_20261023_103203.json`, the iteration-0 run. \** Machine speed varied between days (iteration 6
ran on a slow day; both its before and after were about 60 ms).

**Dev split.** Before this pass it had 64 questions at 100%. The first run on the expanded 112 questions (after 7a/7b,
before 7c) scored 92.9% with 8 failures, all reviewer questions. Now:

| metric | now |
|---|---|
| QA accuracy | 100.0% |
| hallucination | 0.0% |
| wrong-person answers | 0 |
| unanswerable answered confidently | 0.0% |
| Recall@5 | 98.7% |
| intent accuracy | 86.6% |
| escalation recall | 85.7% (unanswerable name questions are now clarified, not escalated, by design) |

Intent accuracy is lower on the new dev questions because they're noisier. The templates' "answer the question asked"
rules absorb most intent errors; the classifier itself was not retrained in this pass.

###### Remaining issues (honest)

1. **The live LLM path is not validated.** No credentials here. Put `ANTHROPIC_API_KEY` in `.env` and run
   `just validate-llm` (about 112 paid calls), then `just eval --llm`. Until then every number above is
   templates-only.
2. **The independent review was by a model, not a person,** and the test split has issues it can't fix while
   frozen: wording-locked keypoints, echo-forbidden items, no similar-name/ambiguous items in test, and
   contamination from retrieval_test. A person should write a v2 test set.
3. **Test 100% is optimistic.** The five fixed failures were identified on the test split.
4. **Name handling is rule-based.** An unlisted ordinary lowercase word after a first name ("dr nisha seated") costs
   one confirmation turn. Unusual phrasings may still slip.
   - Zero tolerance holds on every set here: no answer gives facts about a person other than the one named.
   - That holds only for what the sets contain; it isn't a proof.
5. The intent classifier is unchanged (held-out 0.985, train CV 0.90). Its errors are masked by template guards,
   not fixed.

**Verdict: not production-ready.** Items 1 and 2 are the explicit go-live conditions and remain open: live LLM
validation, and an independent (human) evaluation review. Lowercase name resolution and the regression failures are
addressed and verified on dev, test and the unit tests.

##### Go-live validation (2026-10-24): NOT PRODUCTION-READY

###### 1. Live LLM validation: NOT RUN

- **Credentials:** none on this machine. There is no `.env` file, and no `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`
  in the environment, no `ant` CLI and no local Ollama. The key's presence was checked without printing any value.
- **Not run:** `just eval --llm` (it would have had nothing to call). `just validate-llm` made **no API call**
  ($0.00) and wrote "NOT RUN" to `reports/llm_validation.md`, with the reason, the reproduction steps and a cost
  estimate ($0.86-$5.19 for validate-llm, $0.91-$5.46 for eval --llm).
- **Ready for the first real run:**
  - The adapters count real token usage, priced from `llm.price_*` in settings.
  - The harness reports usage, cost, LLM and end-to-end latency, the near-miss battery and the forced-timeout
    fallback.
  - It only passes if the LLM actually answered: fallbacks outside the timeout probe count as failures.
  - `just eval --llm` now **refuses to run** without an LLM, so template answers can't be reported as a live run.
  - Live results go to separate `*_llm.*` files and are never compared against the template baseline.
- **Simulated only** (real SDK `anthropic` 2.3.0 with a mock HTTP transport, `tests/generation/test_anthropic_adapter.py`):
  refusals, timeouts, malformed and truncated output, 5xx and 401, fallback to templates, wrong-person blocking,
  near-miss marking and request shape. These show our handling, not live API behaviour.

###### 2. Evaluation integrity

- **Frozen test split:** the 120 questions are unchanged (fingerprint `b276955e…`, enforced by a test). No fix in
  this pass touched the system's answering behaviour.
- **`reports/eval_integrity.md`** (`just eval-integrity`) quantifies how blind the test split is:
  - 17 test questions have verbatim or near copies in the retrieval, entity or intent gold sets; 4 more only share a
    sentence pattern with a different room.
  - 16 test items had their failures viewed while fixes were developed.
  - 88 are clean.

  Template-mode QA accuracy with 95% Clopper-Pearson lower bounds:

  | test subset | n | QA accuracy | 95% lower bound |
  |---|---|---|---|
  | all | 120 | 100% | 97.0% |
  | clean | 88 | 100% | 95.9% |
  | copied | 17 | 100% | 80.5% |
  | exposed | 16 | 100% | 79.4% |

  The honest headline is **"100% on 88 clean items, at least 95.9% at 95% confidence, template mode, same author"**,
  not "100%".
- **Wrong-person risk is not measured on the test split:** it has no similar-name or ambiguous items. The dev split
  and the unit tests cover it, and those were developed against.
- **Template-mode and live-LLM results are kept separate everywhere:**
  - every eval report states its mode;
  - live runs use the `_llm` file suffix;
  - the baseline is template mode only.

###### 3. Human gold-set review: NOT DONE

- **Package ready:** `reports/human_review/` (`just review-pack`) holds instructions, CSVs of all 232 QA, 98 retrieval
  and 105 entity items with the gold facts written out, and a separate system-answers file.
- **Sign-off:** the table at the top of `reports/gold_review.md` is empty. The earlier review was by an AI agent and
  is labelled as such; it does not satisfy this condition.

###### 4. Final regression checks

| check | result |
|---|---|
| test suite | **366 passed, 2 skipped** (the 359 from the previous pass, plus 7 go-live safety tests). Skipped: the opt-in live test, and a legacy test that only applies without the SDK. |
| QA, template mode | dev 112 questions: QA 100%, hallucination 0%, wrong-person 0. Test 120: QA 100%, Recall@5 100%, intent 91.7%, hallucination 0%, unanswerable answered 0%, wrong-person 0, p95 19 ms. `just eval` vs baseline: no regression (exit 0). |
| component evals | retrieval Recall@5 1.000 (gold intent); entity F1 1.000, ambiguous 3/3 flagged; intent held-out 0.985 |
| threshold | the dev sweep recommends 0.45 = configured 0.45 (unchanged). The confidence formula, escalation codes, API contract and wrong-person safeguards are unchanged. |
| credentials | none present. A test scans the repo for key-like strings (none found) and checks `.env` is git-ignored and `.env.example` holds no value. Error messages pass through a redactor that removes key values but keeps setting names. There is no git repository on this machine, so "never committed" can't be checked here beyond `.gitignore`. |
| `/health` | Checked on the running API: `llm_ready: false` with the reason "no Anthropic credentials: put ANTHROPIC_API_KEY in .env…". `llm_ready` now reflects the most recent real call, so an invalid or revoked key shows as not ready after the first failure. |

Defects found and fixed in this pass:
- `/health` used to say "ready" whenever a client could be built, even with a bad key.
- A redaction pattern hid the setting name `ANTHROPIC_API_KEY` itself.
- Readiness compared timestamps that can tie on Windows.
- `just eval --llm` without an LLM would have silently produced template results.

###### 5. Go-live decision: **NOT PRODUCTION-READY**

Exact remaining actions, in order:

1. **Provide credentials.** Create `.env` at the repo root with `ANTHROPIC_API_KEY=<key>`; it is git-ignored. Don't
   paste the key into chat, tickets or settings.
2. **`just validate-llm`.** It must report PASS: connectivity, 0 wrong-person, 0 hallucinations, no unexpected
   fallbacks, timeout falls back. Then check the reported cost and latency (the LLM adds a network call; the kiosk
   needs an acceptable p95).
3. **`just eval --llm`.** Read `reports/eval_*_llm.md` next to the template report. Investigate every
   `grounding_failed` block and every fallback. Fix only from dev failures, never from test.
4. **Human review.** An independent person reviews `reports/human_review/` and signs the table in
   `reports/gold_review.md`. Then apply their dev-split corrections and decide how flagged test items are reported
   (they stay frozen).
5. **New test set.** Ideally that person writes a new frozen test set (v2) with similar-name, ambiguous and
   speech-to-text questions in both splits and no copies across gold sets, so wrong-person risk is measured on
   unseen data.
6. **Re-run** `just test`, `just eval`, `just eval-integrity`; mark ready only if every item above passes.

Known residual risks, even after these actions:
- Name handling is rule-based. An unlisted ordinary lowercase word after a first name costs one clarification turn,
  and rare phrasings may slip.
- The intent classifier's errors are masked by "answer the question asked" template guards, not fixed. With the
  LLM on, the LLM answers instead and these guards don't apply, so the live run must be checked for exactly this.
- Classrooms, labs, events, facilities and phone numbers have no data. Those questions escalate by design
  (`reports/data_requests.md`).

##### Proposed, not implemented (root cause seen only on the test split)

**All five rows below were resolved in iteration 7** (see 7b), each through new dev cases.

| failure (test) | root cause | smallest fix |
|---|---|---|
| ~~qa142 "Where is Dr John Mathew sitting?" -> Dr Tegil J John's room~~ | **Resolved in iteration 6** (approved by you; fixed through new dev cases, see below). | |
| qa174 "Who teaches ML? I think it's Dr Sharma." -> "can't find Dr Sharma" | The composer's unknown-name answer wins even when a course in the question can be answered. | Apply the unknown-name answer only for person intents when no other KB entity is linked. |
| qa159 "Where are Dr Nisha Varghese and Dr Deepa V Jose?" -> only the first | The location template uses the first linked person. | Answer each linked person, up to `generation.max_names_spoken`. |
| qa079 "When does Dr Deepa V Jose teach OS?" -> find_by_subject ("Yes, teaches OS") | Intent boundary: "when does <person> teach <course>" is a timetable question. | Labelling-guide line + training examples; or, in the find_by_subject template, answer the schedule days when "when/which day" is asked and schedule facts are retrieved. |
| qa035 "who is Dr Jossy P George" -> find_by_subject -> safe message | Intent flip after retraining; find_by_subject has no template for "only a person linked". | find_by_subject with only a faculty linked: answer from the profile (research areas). |
| qa017 "dr shivangi singh office please" -> consultation hours | Intent flip ("office" read as office hours). | More find_person examples without a question word, or the LLM intent fallback (`intent.llm_fallback_enabled`) once a key is configured. |

To act on these without touching the test split, label a few new dev questions with the same patterns first.

### rag-core (`llm_validation.md`)

#### Live LLM validation

- Run 2026-10-24T08:53:09; provider `anthropic`, model `claude-opus-5-5`, effort `low`, timeout 15 s, retries 1.
- Status: **NOT RUN** (exit 2). No API call was made; cost $0.00.
- Reason: no Anthropic credentials: put ANTHROPIC_API_KEY in .env (see .env.example) or run `ant auth login`
- Consequence: the live LLM path is **unvalidated**. Every reported metric so far comes from template mode (no LLM). Do not treat the system as production-ready on the LLM path.

##### Verified only in simulation (real SDK + mock HTTP transport, no network)

`tests/generation/test_anthropic_adapter.py` drives the installed `anthropic` SDK and our adapter through scripted
HTTP responses. These pass, but they are **simulated**: they prove our handling of each response type, not how the
live API behaves.

| behaviour | result |
|---|---|
| request shape (model, structured output schema, effort, refusal fallbacks, no temperature) | passes (simulated) |
| malformed JSON -> one retry with the error -> success / second failure -> error | passes (simulated) |
| `stop_reason: refusal` / `max_tokens` -> error -> templates answer | passes (simulated) |
| timeout -> one SDK retry -> error -> templates answer | passes (simulated) |
| 5xx -> retried and recovered; 529 overloaded -> templates answer; 401 -> no retry | passes (simulated) |
| LLM answer about the wrong person (near miss) -> blocked as `grounding_failed` | passes (simulated) |
| near miss marked "a DIFFERENT person" in the prompt; prompt rule 11 present | passes (simulated) |
| a fake API key absent from results, query log, health status, settings repr | passes (simulated) |

Verified for real on this machine, without credentials: the installed SDK resolves no credentials, the running API's
`/health` reports `llm_ready: false` with the reason, and answers come from the templates (2026-10-24).

##### Reproduce

1. Put the key in `.env` at the repo root: `ANTHROPIC_API_KEY=...` (git-ignored; never in settings.yaml, code, tickets or chat). Install the extra once: `pip install -e .[anthropic]`.
2. `just validate-llm` -> this report (dev split, live).
3. `just eval --llm` -> `reports/eval_<timestamp>_llm.md/.json` + `failures_llm.csv` (test split, live). Template-mode reports keep their own names; never compare the two as one series.

Estimated cost at $4/$20 per million input/output tokens (`llm.price_*` in settings): validate-llm about $0.86-$5.19 (114 calls), eval --llm about $0.91-$5.46 (120 calls). The upper bound assumes every reply uses `llm.max_tokens`. A real run reports its actual token usage and cost in the status lines at the top.

### voice-memory (`voice_metrics.md`)

#### Voice + memory metrics

Source: `C:\Projects\Smart_University\voice-memory\logs\turns.jsonl`, 90 turns.

##### Response latency (ms)

| stage | n | p50 | p95 | max |
|---|---|---|---|---|
| stt | 12 | 6565.1 | 10109.0 | 10274.6 |
| memory | 90 | 0.2 | 0.9 | 4.5 |
| rag | 90 | 43.7 | 136.1 | 231.3 |
| handoff | 12 | 0.1 | 1.0 | 1.3 |
| tts | 90 | 0.1 | 519.7 | 6352.6 |
| total | 90 | 44.9 | 9500.0 | 10412.7 |
| total (text input) | 78 | 42.2 | 687.0 | 6431.5 |
| total (voice input) | 12 | 6689.4 | 10298.7 | 10412.7 |

p95 total 9500.0 ms MISSES the target of 3000 ms.

##### Query completion rate

| | count |
|---|---|
| queries | 84 |
| completed by the receptionist | 72 |
| escalated / handed off | 12 |
| abandoned at a clarification | 0 |
| sessions fully completed | 49 of 61 |

Completion rate **85.7%**, which meets the target of 85%.

Escalations by reason:

- `not_in_kb`: 10
- `user_requested`: 2

Follow-ups rewritten by memory: 13.6% of queries.
