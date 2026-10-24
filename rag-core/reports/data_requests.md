# Data to collect from the department

These gaps can't be fixed in code: the assistant escalates instead of guessing (golden rule). On the QA test split,
33 of 120 questions (28%) escalate as `not_in_kb`; most of them because the data below doesn't exist yet. Each
item says what to collect, in what form, and which questions it unlocks. Put verified files under `data/raw/` (or
edit `data/kb/glossary.yaml` for names), then `just index` (or `POST /reload-kb`).

## 1. Classrooms and labs (highest impact)

- **What:** the room for each class: programme + semester + section (+ stream for BSc) + subject -> room number,
  block, floor. The timetable lines have no room at all ("Timetable lines have no room", CLAUDE.md).
- **Also:** the labs on the floor (name, room, which classes use them).
- **Form:** a sheet with columns `programme, semester, section, stream, subject_code, day, start, end, room, block, floor`
  (one row per timetable line), or a simpler `programme, semester, section -> home classroom` table.
- **Unlocks:** "Where is the MSc AI and ML classroom?" (the problem statement's own example), "Where is the ML class?",
  "Where is the AI lab?". 6 QA questions today, all escalated.

## 2. Subject and programme names (glossary verification)

Nothing in `data/kb/glossary.yaml` is `verified: true` yet, so every answer says the code ("ML is taught by...")
and course facts count as unverified (lower confidence).

- **Confirm (verified: true) or correct these expansions:** CN = Computer Networks, DBMS = Database Management
  Systems, DSA = Data Structures and Algorithms, IoT = Internet of Things, ML = Machine Learning, OS = Operating
  Systems, RM = Research Methodology, SE = Software Engineering; programmes BCA, BSC, MCA, MSAIM
  (= MSc Artificial Intelligence and Machine Learning?).
- **Provide full names for these codes (currently `TODO`):** ACTIVITY, ADA, AMI, BI, BT/DF, C PROG, CPCG, CSA, CV,
  DAA, DEV, DF, DLD, DV, FSD, JAVA, MDC, MOP, MOT, NDM, PP, PRJ, PYTHON, RPA, SP, SPD, SPR, TML, UI/UX, WSD; programme MSC.
- **Confirm aliases:** are "MSc AIML", "MSc AI and ML" and timetable code "MSAIM" the same programme? Is `MSC CSA`
  (Dr Sandeep J, Saturday) a programme or a subject? Who is `SG` (co-teacher in the BI timetable, not in the faculty list)?
- **Unlocks:** "What is TML?" answered with a name instead of "a subject code I can't verify", and visitors who ask by
  subject name ("who teaches design and analysis of algorithms") instead of by code.

## 3. Missing faculty fields

| field | faculty | unlocks |
|---|---|---|
| Email | Dr Kavitha R, Dr Nizar Banu P K, Dr Suresh K | "What is Dr Kavitha R's email?" (escalates today) |
| Consultation hours | Dr Tegil J John, Dr Kavitha R, Dr Jobin Francis, Dr Jossy P George, Dr Limna Das P, Dr Nizar Banu P K, Dr Rupali Sunil Wagh, Dr Suresh K | "When can I meet Dr Jobin Francis?" |
| Consultation hours that can't be read | Dr Syam Mohan E ("Monday", no time), Dr Peter Augustin D ("1:30 PM to 2:30 3:30 to 4:30"), Dr Naived George Eapen and Dr Shoney Sebastian (time, no day), Dr Logeshwaran J (PG/UG times, no days), Dr Manasa N Kulkarni (start times only), Dr Sandeep J, Dr Saravanan K N, Dr Surabhi Saxena, Dr Vijay Arputharaj J (partly unreadable) | "Is Dr Syam Mohan free now?" (today answered with a caveat) |
| Office location | Dr Suresh K (no block, floor or room); Dr Shoney Sebastian ("Director, Center for Research (PhD)" is a designation, not a room) | "Where is Dr Suresh K's cabin?" |
| Research areas | Dr Neetha S S, Dr Shivangi Singh, Dr Syam Mohan E, Dr Somnath Sinha, Dr Jossy P George, Dr Limna Das P, Dr Peter Augustin D, Dr Ramamurthy B, Dr Thirunavukkarasu V | research-area questions |

## 4. Conflicting records to resolve

- **Dr Helen K Joy:** listed in room 885; the notes say the main list also has room 875 (Dr Sridevi R's room). Which is right?
- **Dr Thirunavukkarasu V:** room 879 is noted as "Central Block (old)". Still current?
- **`Currently Handling` vs the timetable:** several rows say UG while the timetable shows MCA/MSc AIML classes.

The assistant states both values today and lowers its confidence.

## 5. Categories with no data at all

| category | what to collect | QA questions today |
|---|---|---|
| Events (seminars, workshops, guest lectures, orientation) | a maintained list: title, date, time, venue room, audience | 5, all escalated |
| Facilities (washroom, drinking water, wifi, printing, lift, staff room, waiting area) | location (room/near which room) and status/hours; wifi: what visitors may be told | 6, all escalated |
| Department office / HOD | who the HOD is, the department office room, its hours and official contact | "Who is the HOD of computer science?" |

## 6. Policy decisions

- **Phone numbers:** the sheet has none, so every phone question escalates (5 QA questions + adversarial ones). Should
  the kiosk give an office extension? If yes, provide the official department number(s), not personal mobiles.
- **Notes column:** curator notes refer to a "main list" and a "short-names sheet" that we don't have. Sharing them would
  let us check the conflicts above.
