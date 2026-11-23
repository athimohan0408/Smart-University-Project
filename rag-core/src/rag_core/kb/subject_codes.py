"""Subject full forms from the dataset's "Subject Codes" sheet -> data/kb/glossary.yaml.

The department's workbook (``paths.raw_faculty_xlsx``) has a "Subject Codes" sheet: each timetable code with its
full form and a Status. A code whose Status is "Done" is the department's answer, so its glossary entry gets that
full form and ``verified: true`` (answers then say "TinyML (TML)"). Anything else is left as it is in the glossary.

Run after the sheet changes: ``python -m rag_core.kb.subject_codes`` (``just sync-glossary``), then ``just index``.
``--check`` only reports differences (exit 1 if any); a test runs it, so the two can't drift apart silently.
Only the ``full_name`` and ``verified`` lines of existing entries are edited, so the file's comments stay.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

SHEET = "Subject Codes"
CODE_COL = "Code (as in the timetable)"
FULL_COL = "Full form"
STATUS_COL = "Status"


def _glossary_code(code: str) -> str:
    """The sheet writes "MSC CSA" as one code; the timetable parser reads programme MSC + subject CSA."""
    return "CSA" if code == "MSC CSA" else code


def read_subject_codes(xlsx: Path) -> dict[str, str]:
    """Code -> full form, for rows the department marked Done. Empty when the sheet is absent."""
    try:
        df = pd.read_excel(xlsx, sheet_name=SHEET, dtype=str)
    except ValueError:
        return {}
    out = {}
    for _, row in df.iterrows():
        code, full, status = row.get(CODE_COL), row.get(FULL_COL), row.get(STATUS_COL)
        if not isinstance(code, str) or not isinstance(full, str) or not full.strip():
            continue
        if isinstance(status, str) and status.strip().casefold() == "done":
            out[_glossary_code(" ".join(code.split()))] = " ".join(full.split())
    return out


def sync_glossary_text(text: str, codes: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """Returns (new text, changed codes, codes not in the glossary)."""
    lines = text.split("\n")
    try:
        start = lines.index("subjects:")
    except ValueError as exc:
        raise ValueError("glossary has no 'subjects:' section") from exc
    changed, missing = [], []
    for code, full in codes.items():
        header = f"  {code}:"
        try:
            i = lines.index(header, start)
        except ValueError:
            missing.append(code)
            continue
        wanted = {"    full_name: ": f"    full_name: {full}", "    verified: ": "    verified: true"}
        j = i + 1
        while j < len(lines) and lines[j].startswith("    "):
            for prefix, line in wanted.items():
                if lines[j].startswith(prefix) and lines[j] != line:
                    lines[j] = line
                    changed.append(code)
            j += 1
    return "\n".join(lines), sorted(set(changed)), missing


def main(argv: list[str] | None = None) -> int:
    from rag_core.config import get_settings

    parser = argparse.ArgumentParser(description="Copy subject full forms from the dataset into the glossary.")
    parser.add_argument("--check", action="store_true", help="report differences only; exit 1 if any")
    args = parser.parse_args(argv)
    paths = get_settings().paths
    codes = read_subject_codes(paths.raw_faculty_xlsx)
    text = paths.glossary.read_text(encoding="utf-8")
    new, changed, missing = sync_glossary_text(text, codes)
    for code in missing:
        print(f"not in the glossary (add it by hand first): {code}")
    if args.check:
        for code in changed:
            print(f"differs from the sheet: {code} -> {codes[code]}")
        return 1 if (changed or missing) else 0
    paths.glossary.write_text(new, encoding="utf-8")
    print(f"{len(codes)} codes from {paths.raw_faculty_xlsx.name} [{SHEET}]; updated: {', '.join(changed) or 'none'}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
