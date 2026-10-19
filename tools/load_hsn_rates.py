"""Load official GST goods rates into the Supabase `hsn_rates` table.

Sources are CBIC notifications kept in data/notifications/, downloaded from the CBIC Tax Information Portal
(https://taxinformation.cbic.gov.in/api/cbic-notification-msts/download/<id>/ENG, the PDF is base64 in "data"):
  9-2025-CT-Rate.pdf   id 1010436  rate schedules, in force from 22 Sep 2025
  10-2025-CT-Rate.pdf  id 1010437  exempted goods (nil rate), in force from 22 Sep 2025
Later amendments are applied from AMENDMENTS below. Re-run after adding a new one:

    python -m tools.load_hsn_rates           # parse and replace the table
    python -m tools.load_hsn_rates --dry-run # parse and print, write nothing
"""
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

from backend import db

FOLDER = Path(__file__).parent.parent / "data" / "notifications"
SCHEDULE = re.compile(r"^Schedule\s+([IVX]+)\s*[–-]\s*([\d.]+)\s*%", re.I)
SERIAL = re.compile(r"^\d+[A-Z]?\.$")

SOURCES = [
    ("9-2025-CT-Rate.pdf", "9/2025-Central Tax (Rate)", "2025-09-22"),
    ("10-2025-CT-Rate.pdf", "10/2025-Central Tax (Rate)", "2025-09-22"),
]

# Amendments to 9/2025-Central Tax (Rate). Rates here are the total GST rate (CGST + SGST).
AMENDMENTS = [
    # 19/2025-Central Tax (Rate) dated 31 Dec 2025, in force from 1 Feb 2026
    {"insert": {"schedule": "II", "serial": "4A", "rate": 18, "hsn_entry": "2403 19 21, 2403 19 29",
                "description": "Biris"}, "by": "19/2025-Central Tax (Rate)", "from": "2026-02-01"},
    {"insert": {"schedule": "III", "serial": "14", "rate": 40, "hsn_entry": "2106 90 20",
                "description": "Pan masala"}, "by": "19/2025-Central Tax (Rate)", "from": "2026-02-01"},
    {"insert": {"schedule": "III", "serial": "15", "rate": 40, "hsn_entry": "2401",
                "description": "Unmanufactured tobacco; tobacco refuse [other than tobacco leaves]"},
     "by": "19/2025-Central Tax (Rate)", "from": "2026-02-01"},
    {"insert": {"schedule": "III", "serial": "16", "rate": 40, "hsn_entry": "2402",
                "description": "Cigars, cheroots, cigarillos and cigarettes, of tobacco or of tobacco substitutes"},
     "by": "19/2025-Central Tax (Rate)", "from": "2026-02-01"},
    {"insert": {"schedule": "III", "serial": "17", "rate": 40, "hsn_entry": "2403 (other than 2403 19 21, 2403 19 29)",
                "description": "Other manufactured tobacco and manufactured tobacco substitutes; homogenised or "
                               "reconstituted tobacco; tobacco extracts and essences [other than biris]"},
     "by": "19/2025-Central Tax (Rate)", "from": "2026-02-01"},
    {"insert": {"schedule": "III", "serial": "18", "rate": 40, "hsn_entry": "2404 11 00",
                "description": "Products containing tobacco or reconstituted tobacco and intended for inhalation "
                               "without combustion"}, "by": "19/2025-Central Tax (Rate)", "from": "2026-02-01"},
    {"insert": {"schedule": "III", "serial": "19", "rate": 40, "hsn_entry": "2404 19 00",
                "description": "Products containing tobacco or nicotine substitutes and intended for inhalation "
                               "without combustion"}, "by": "19/2025-Central Tax (Rate)", "from": "2026-02-01"},
    {"omit_schedule": "VII", "by": "19/2025-Central Tax (Rate)", "from": "2026-02-01"},
    # 01/2026-Central Tax (Rate) dated 30 Apr 2026, in force from 1 May 2026, with corrigendum of 6 May 2026
    {"set_hsn": {"schedule": "I", "serial": "150", "hsn_entry": "2202 99 21, 2202 99 29"},
     "by": "01/2026-Central Tax (Rate)", "from": "2026-05-01"},
    {"set_hsn": {"schedule": "I", "serial": "151", "hsn_entry": "22029931, 22029939"},
     "by": "01/2026-Central Tax (Rate)", "from": "2026-05-01"},
    {"set_hsn": {"schedule": "III", "serial": "2", "hsn_entry": "2202 91 00, 2202 99 91, 2202 99 99"},
     "by": "01/2026-Central Tax (Rate)", "from": "2026-05-01"},
    {"set_hsn": {"schedule": "III", "serial": "3", "hsn_entry": "2202 99 91, 2202 99 99"},
     "by": "01/2026-Central Tax (Rate)", "from": "2026-05-01"},
]


def lines_of(page):
    """Words of a page grouped into lines, top to bottom, each line left to right."""
    lines = {}
    for w in page.get_text("words"):
        x, y, word = w[0], round(w[1]), w[4]
        key = next((k for k in lines if abs(k - y) <= 2), y)
        lines.setdefault(key, []).append((x, word))
    return [sorted(words) for _, words in sorted(lines.items())]


def columns(lines, previous):
    """Left edges of the serial and description columns on a page: the most common line starts."""
    serials = [round(words[0][0]) for words in lines if SERIAL.match(words[0][1])]
    starts = [round(x) for words in lines for x, _ in words if 200 < x < 320]
    if not serials or not starts:
        return previous
    return min(serials), Counter(starts).most_common(1)[0][0]


def parse_notification(path):
    """Rows of (schedule, total rate, serial, HSN column text, description) from a rate or exemption notification."""
    rows, schedule, rate, row, last, edges = [], "Exempt", 0.0, None, 0, (0, 0)
    for page in pymupdf.open(path):
        lines = lines_of(page)
        serial_x, desc_x = edges = columns(lines, edges)
        for words in lines:
            heading = SCHEDULE.match(" ".join(w for _, w in words))
            if heading:
                schedule, rate, row, last = heading.group(1), round(float(heading.group(2)) * 2, 3), None, 0
                continue
            first_x, first = words[0]
            if first_x <= serial_x + 10 and SERIAL.match(first):
                number = int(re.match(r"\d+", first).group())
                if number < last:
                    return finish(rows)  # numbering restarted: the lists appended after the schedule
                last = number
                row = {"schedule": schedule, "rate": rate, "serial": first.rstrip("."), "hsn": [], "desc": []}
                rows.append(row)
                words = words[1:]
            elif first_x <= serial_x + 10:
                row = None  # explanations, notes and the signature start at the left margin
            if row:
                row["hsn"] += [w for x, w in words if x < desc_x - 5]
                row["desc"] += [w for x, w in words if x >= desc_x - 5]
    return finish(rows)


def finish(rows):
    return [{"schedule": r["schedule"], "rate": r["rate"], "serial": r["serial"],
             "hsn_entry": " ".join(r["hsn"]), "description": " ".join(r["desc"])} for r in rows]


def codes(hsn_entry):
    """Split a column-(2) entry into the codes it covers and the codes it excludes ("other than", "Except")."""
    exclusion = re.compile(r"[(\[]?\s*\b(?:other than|except)\b([\d\s,]*(?:or[\d\s,]+)*)[)\]]?", re.I)
    excluded = " , ".join(exclusion.findall(hsn_entry))
    included = exclusion.sub(" ", hsn_entry)
    found = []
    for part in re.split(r",|;|\band\b|\bor\b", included):
        digits = re.sub(r"\D", "", part)
        # two 8-digit tariff items printed without a comma between them
        found += [digits[i:i + 8] for i in range(0, len(digits), 8)] if len(digits) > 8 and len(digits) % 8 == 0 else [digits]
    split_excluded = [re.sub(r"\D", "", part) for part in re.split(r",|\bor\b", excluded)]
    return [c for c in found if 2 <= len(c) <= 8], [c for c in split_excluded if c]


def build():
    entries = []
    for file, notification, effective in SOURCES:
        for row in parse_notification(FOLDER / file):
            entries.append({**row, "notification": notification, "effective_from": effective})
    main = "9/2025-Central Tax (Rate)"
    for change in AMENDMENTS:
        note = {"notification": f"{main}, as amended by {change['by']}", "effective_from": change["from"]}
        if "insert" in change:
            entries.append({**change["insert"], **note})
        elif "omit_schedule" in change:
            entries = [e for e in entries if not (e["notification"].startswith(main) and e["schedule"] == change["omit_schedule"])]
        else:
            target = change["set_hsn"]
            for e in entries:
                if e["notification"].startswith(main) and e["schedule"] == target["schedule"] and e["serial"] == target["serial"]:
                    e.update(hsn_entry=target["hsn_entry"], **note)

    rows = []
    for e in entries:
        included, excluded = codes(e["hsn_entry"])
        rows += [{
            "hsn": code, "excludes": excluded, "hsn_entry": e["hsn_entry"], "description": e["description"],
            "gst_rate": e["rate"], "notification": e["notification"], "schedule": e["schedule"],
            "serial": e["serial"], "effective_from": e["effective_from"],
        } for code in included]
    return entries, rows


if __name__ == "__main__":
    entries, rows = build()
    print(f"{len(entries)} notification entries -> {len(rows)} HSN rows")
    no_code = [e for e in entries if not codes(e["hsn_entry"])[0]]
    print(f"{len(no_code)} entries without an HSN code (not loaded):")
    for e in no_code:
        print(f"  {e['notification']} Sch {e['schedule']} S.No {e['serial']}: {e['hsn_entry']!r} {e['description'][:60]!r}")
    wordy = {e["hsn_entry"] for e in entries if re.search(r"[a-z]{3,}", re.sub(r"(?i)other than|except|and|or", "", e["hsn_entry"]))}
    print(f"{len(wordy)} HSN entries containing words, check by eye:", sorted(wordy))
    if "--dry-run" in sys.argv:
        for code in ("2501", "3402", "1006", "0713"):
            for r in rows:
                if r["hsn"] == code:
                    print(f"  {code}: {r['gst_rate']}% Sch {r['schedule']} S.No {r['serial']} {r['description'][:70]!r}")
        sys.exit()
    db.call("DELETE", "/rest/v1/hsn_rates", params={"id": "gte.0"})
    for i in range(0, len(rows), 500):
        db.call("POST", "/rest/v1/hsn_rates", json=rows[i:i + 500])
    print(f"Loaded {len(rows)} rows into hsn_rates.")
