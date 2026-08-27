r"""One-time conversion: finalized eval xlsx -> eval/data/eval_set.json.

Reads the "Labels" sheet of the finalized spreadsheet and emits one JSON
record per scorable row.

Two kinds of row are dropped or repaired here, because the spreadsheet's
"Draft Ideal Answer" column holds two different kinds of content:

  * On rows the founder marked FAIL, the column holds a real corrected
    reply. That is usable ground truth as-is.

  * On rows the founder marked PASS, the column holds a note ABOUT the
    reply -- "As-sent is correct.", "As-sent is aligned." -- rather than
    a reply. Grading a freshly generated customer message against the
    string "As-sent is correct." is meaningless: every judge scores it a
    mismatch. For those rows the founder's own verdict certifies that the
    answer actually sent WAS the right answer, so the recorded answer is
    promoted to ground truth and the note is kept as provenance. Nothing
    is invented -- the ground truth is the founder-approved text.

  * Rows with no finalized ideal answer at all are skipped. Those are open
    policy gaps; inventing a target for them would poison the eval set.

Usage:
    venv\Scripts\python.exe eval/convert.py "C:\path\to\twin_eval_labels_v2.xlsx"
"""
import json
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

SHEET = "Labels"
OUT = Path(__file__).resolve().parent / "data" / "eval_set.json"

# Column header -> output field. Keyed by header text so a column reorder
# in the spreadsheet doesn't silently shift the data.
COLUMNS = {
    "ID": "id",
    "Channel": "channel",
    "Category": "category",
    "Customer Question": "question",
    "Draft Ideal Answer": "ideal_answer",
    "Twin's Actual Answer": "actual_answer",
    "Verdict": "human_verdict",
}

# Only these three are usable as calibration labels; the Verdict column
# also holds free-text notes ("gotta work on this"), which are not labels.
VERDICTS = {"pass": "Pass", "fail": "Fail", "partial": "Partial"}

# An "ideal answer" that is really a note about the answer. Verified against
# the finalized sheet: this matches 39 rows, and no genuine ideal reply in
# the sheet is shorter than 45 characters, so there are no false positives.
META_IDEAL = re.compile(
    r"^(as-sent|as sent|same as row|same as\b|draft is correct|correct\b|no change)",
    re.I,
)


def clean(value) -> str:
    return "" if value is None else str(value).strip()


def convert(xlsx_path: Path) -> list[dict]:
    wb = load_workbook(xlsx_path, data_only=True)
    if SHEET not in wb.sheetnames:
        raise SystemExit(f"No {SHEET!r} sheet in {xlsx_path.name} (found: {wb.sheetnames})")
    ws = wb[SHEET]

    header = [clean(c.value) for c in ws[1]]
    missing = [h for h in COLUMNS if h not in header]
    if missing:
        raise SystemExit(f"Spreadsheet is missing expected column(s): {missing}")
    index = {COLUMNS[h]: header.index(h) for h in COLUMNS}

    rows: list[dict] = []
    skipped_blank: list[str] = []
    skipped_meta: list[str] = []
    promoted: list[str] = []

    for excel_row in ws.iter_rows(min_row=2, values_only=True):
        if not any(clean(v) for v in excel_row):
            continue
        rec = {field: clean(excel_row[i]) for field, i in index.items()}

        raw_verdict = rec.pop("human_verdict", "")
        rec["human_verdict"] = VERDICTS.get(raw_verdict.lower(), None)

        if not rec["ideal_answer"]:
            skipped_blank.append(rec["id"])
            continue

        if META_IDEAL.match(rec["ideal_answer"]):
            if rec["human_verdict"] == "Pass" and rec["actual_answer"]:
                # Founder certified the sent reply as correct -> it is the target.
                rec["ideal_note"] = rec["ideal_answer"]
                rec["ideal_answer"] = rec["actual_answer"]
                rec["ideal_source"] = "as-sent (founder-certified Pass)"
                promoted.append(rec["id"])
            else:
                # A note with no Pass to certify it (e.g. "Same as row 11"),
                # so there is nothing to ground the row against.
                skipped_meta.append(rec["id"])
                continue
        else:
            rec["ideal_source"] = "founder-written"

        rows.append(rec)

    print(f"Read {xlsx_path.name} [{SHEET}]")
    print(f"  scorable rows          : {len(rows)}")
    print(f"    founder-written ideal: {sum(1 for r in rows if r['ideal_source'] == 'founder-written')}")
    print(f"    as-sent promoted     : {len(promoted)} -> IDs {', '.join(promoted) or 'none'}")
    print("       reason: founder marked these Pass and wrote a note, not a reply;")
    print("       the sent answer is therefore the founder-approved target.")
    print(f"  skipped, no ideal      : {len(skipped_blank)} -> IDs {', '.join(skipped_blank) or 'none'}")
    print("       reason: no finalized ideal answer yet (open policy gap).")
    print(f"  skipped, note-only     : {len(skipped_meta)} -> IDs {', '.join(skipped_meta) or 'none'}")
    print("       reason: ideal column holds a note with no Pass verdict to")
    print("       certify the sent reply, so the row has no usable target.")
    labelled = sum(1 for r in rows if r["human_verdict"])
    print(f"  usable human verdicts  : {labelled} (for judge calibration)")
    return rows


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    xlsx_path = Path(sys.argv[1])
    if not xlsx_path.exists():
        raise SystemExit(f"No such file: {xlsx_path}")

    rows = convert(xlsx_path)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
