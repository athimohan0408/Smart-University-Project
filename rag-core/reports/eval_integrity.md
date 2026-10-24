# Evaluation integrity: how blind is the frozen test split?

- Test split: 120 questions (byte-for-byte frozen; fingerprint checked by `tests/eval/test_qa_eval.py::test_test_split_is_frozen`).
- **17** test questions have a verbatim or near copy (token_sort_ratio >= 85, same numbers) in another gold set used during development; 4 more only share a sentence pattern with a different room/number (listed, not counted as copies).
- **16** test items had their failures viewed while fixes were developed.
- **88** test items are clean (neither copied nor exposed).

## Similar questions in other gold sets

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

## Exposed test items (failures viewed during development)

qa017, qa024, qa035, qa079, qa086, qa090, qa107, qa118, qa120, qa123, qa142, qa143, qa146, qa159, qa170, qa174

Fixes for these were general rules backed by new dev cases with other names and wording; no test item was edited. But their patterns were seen, so their current pass rate is not an unbiased estimate.

## Effect on the reported numbers (template mode, test split)

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
