# Gold-set review: instructions for the human reviewer

**Status: NOT YET REVIEWED.** An AI agent has already looked at these sets (its notes are in `../gold_review.md`),
but that does **not** count as this review. We need an independent person to check them before the system can go
live.

## Who should review

Ideally someone who knows the Computer Science department on the 8th floor of Central Block, CHRIST University, and
the kind of questions visitors ask at the front desk. That could be a receptionist, a staff member, or a student
volunteer. You must not have worked on this system.

## What you are checking

The kiosk answers visitors' questions about faculty (where they sit, consultation hours, emails, who teaches what)
from one spreadsheet of faculty data. We grade it against "gold" questions with known right answers. Please tell us
where those right answers are wrong, too strict, too loose, or unrealistic.

## Files (open in Excel or LibreOffice)

| file | what it is | your columns |
|---|---|---|
| `qa_review.csv` | Every test question, with the facts the answer must come from written out in full in the **gold fact text** column. **split = test** rows are frozen: we can't change them, but your verdicts decide how we read their scores. | R1-R7 |
| `retrieval_review.csv` | Which facts the system should find for each question | R1-R3 |
| `entities_review.csv` | Which people, rooms, subjects and days a question mentions | R1-R2 |
| `system_answers.csv` | What the system answered. **Open this only after you've finished `qa_review.csv`**, so it doesn't influence you. | R1 |

The original faculty sheet is `data/raw/8th_floor_faculty_final_data.xlsx`. If it contradicts the gold fact text,
the gold is wrong: please say so.

## Please check, for each row in `qa_review.csv`

1. **R1, facts correct.** Are the gold facts and keypoints true according to the faculty sheet (rooms, emails,
   times, names)?
2. **R2, answerable.** "no" means the kiosk must hand over to the human receptionist, or ask a clarifying question,
   and give no facts. Do you agree? For example: phone numbers aren't in the data, so questions about them should
   say "no".
3. **R3, keypoints.** Keypoints are words a correct answer must contain. Are they both needed and enough? A correct
   answer must not fail just because of wording. Example: "isn't on the 8th floor" should count as well as "not on
   this floor".
4. **R4, acceptable paraphrases.** List other wordings of the key facts you would accept: "9th floor" for "Ninth
   Floor", "R&D Block" for "R & D Block", "2 to 4 pm" for "2 pm to 4 pm".
5. **R5, names.** Rows with difficulty `similar_name`, `ambiguous`, `asr_noise` or `misspelt` test names:
   - A name like "Dr Nisha Sharma" is NOT Dr Nisha Varghese. The kiosk must not give her room; it may ask "did you
     mean…?".
   - A name that fits two people ("Dr George") must lead to a question, not a pick.
   - Speech-to-text errors ("shyam mohan" for Syam Mohan E, "sri devi") should still reach the right person.

   Do the gold answers get this right?
6. **R6, intent.** The intent labels follow the rules in `CLAUDE.md` (section "NLU: intent"). Flag clear mistakes
   only.
7. **R7, notes.** Also tell us if a question isn't how a real visitor would talk, if something important is
   missing, or anything else.

Leave a column empty when it doesn't apply. A useful pass is the 120 `test` rows plus a sample of `dev` rows; all
232 rows take roughly 3-4 hours.

## When you're done

Return the filled-in CSVs, and fill in the sign-off table in `../gold_review.md` (name, date, which files and rows,
overall verdict). The review only counts as done once that table is signed.
