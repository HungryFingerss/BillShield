"""Deterministic bill reading: file type -> text lines -> amounts -> findings. No LLM here."""
import io
import re
from difflib import SequenceMatcher
from functools import cache

import pymupdf
from PIL import Image

SIGNATURES = {b"%PDF": "application/pdf", b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg"}
MAX_PAGES = 10

# An amount at the end of a line: "Rs. 1,532.82", "699.00", "499", "100/-".
AMOUNT = re.compile(
    r"(?<![\w/.:-])(?:rs\.?|inr|₹|\$)?\s*(-?\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|-?\d{1,7}(?:\.\d{1,2})?)\s*(?:/-)?\s*$",
    re.I,
)
IGNORE = re.compile(r"\b(invoice|account|a/c|phone|mobile|tel|pin|pincode|gstin|pan|hsn|sac|date)\b", re.I)
SUBTOTAL = re.compile(r"sub\s*-?\s*total", re.I)
TAX = re.compile(r"\b(gst|cgst|sgst|igst|vat|tax|cess)\b", re.I)
TOTAL = re.compile(r"total|amount due|payable|balance due", re.I)
DISCOUNT = re.compile(r"discount|rebate|waiver|cashback", re.I)
FILLER = {"charge", "charges", "fee", "fees", "rs", "inr", "the", "of", "for", "and", "per", "month", "monthly"}


LOW_RESOLUTION = 1000  # pixels on the longer side; below this OCR starts misreading small print like the ₹ sign


def is_low_resolution(data, mime):
    if mime == "application/pdf":
        return False
    with Image.open(io.BytesIO(data)) as image:
        return max(image.size) < LOW_RESOLUTION


def file_type(data):
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return next((mime for sig, mime in SIGNATURES.items() if data.startswith(sig)), None)


@cache
def ocr_engine():
    from rapidocr import RapidOCR

    return RapidOCR()


def ocr(image_bytes):
    result = ocr_engine()(image_bytes)
    if result.boxes is None:
        return []
    return [(box[0][0], box[1][0], box[0][1], box[2][1], text) for box, text in zip(result.boxes, result.txts)]


def read_pages(data, mime):
    """Text pieces with their position, per page: (left, right, top, bottom, text)."""
    if mime != "application/pdf":
        return [ocr(data)]
    pages = []
    for page in list(pymupdf.open(stream=data, filetype="pdf"))[:MAX_PAGES]:
        words = [(w[0], w[2], w[1], w[3], w[4]) for w in page.get_text("words")]
        pages.append(words or ocr(page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")))  # scanned page
    return pages


def group_lines(fragments):
    """Join fragments that sit on the same row into lines, left to right. A gap wider than about two characters'
    height marks a new column and is written as " | ", so tables keep their shape in plain text."""
    rows = []
    for fragment in sorted(fragments, key=lambda f: (f[2] + f[3]) / 2):
        top, bottom = fragment[2], fragment[3]
        middle = (top + bottom) / 2
        if rows and abs(middle - rows[-1]["middle"]) <= (bottom - top) / 2:
            rows[-1]["parts"].append(fragment)
        else:
            rows.append({"middle": middle, "parts": [fragment]})
    lines = []
    for row in rows:
        parts = sorted(row["parts"])
        line = parts[0][4]
        for before, part in zip(parts, parts[1:]):
            line += (" | " if part[0] - before[1] > 2 * (part[3] - part[2]) else " ") + part[4]
        lines.append(line)
    return lines


def read_lines(pages):
    return [line for page in pages for line in group_lines(page)]


GST_COLUMN = re.compile(r"\b[CSIU]?GST\b|UTGST", re.I)
HSN_CODE = re.compile(r"^\d{4,8}$")


def header_columns(header):
    """Group header words into columns: words side by side on one line, or stacked over each other."""
    columns = []
    for left, right, top, _, text in sorted(header, key=lambda f: (f[2], f[0])):
        for col in columns:
            same_line = abs(col["top"] - top) < 2 and 0 <= left - col["right"] < 4
            stacked = left < col["right"] and right > col["left"]
            if same_line or stacked:
                col.update(left=min(col["left"], left), right=max(col["right"], right), top=top)
                col["words"].append(text)
                break
        else:
            columns.append({"left": left, "right": right, "top": top, "words": [text]})
    for col in columns:
        col["label"] = " ".join(col["words"])
    return sorted(columns, key=lambda c: c["left"])


def column_of(fragment, columns, desc):
    """The column a value belongs to: the description takes everything between its neighbours, others the nearest."""
    centre = (fragment[0] + fragment[1]) / 2
    if desc is not None:
        i = columns.index(desc)
        before = columns[i - 1]["right"] if i else 0
        after = columns[i + 1]["left"] if i + 1 < len(columns) else 10 ** 6
        if before < centre < after:
            return desc
    return min(columns, key=lambda c: max(c["left"] - centre, centre - c["right"], 0))


def numbers(texts):
    joined = " ".join(texts).replace(",", "")
    rates = [float(r) for r in re.findall(r"(\d+(?:\.\d+)?)\s*%", joined)]
    amounts = [float(a) for a in re.findall(r"(?<![\d.])(\d+\.\d{2})(?!\s*%|\d)", joined)]
    return rates, amounts


def gst_rows(pages):
    """Line items of a tax-invoice table that has an HSN column: description, HSN, GST rate, taxable value, tax."""
    rows = []
    for page in pages:
        hsn_word = next((f for f in page if f[4].upper().startswith("HSN")), None)
        if not hsn_word:
            continue
        codes = sorted((f for f in page if f[2] > hsn_word[3] and HSN_CODE.match(f[4])
                        and f[0] < hsn_word[1] + 15 and f[1] > hsn_word[0] - 15), key=lambda f: f[2])
        if not codes:
            continue
        header = [f for f in page if hsn_word[2] - 30 < f[2] < min(hsn_word[3] + 18, codes[0][2] - 2)]
        columns = header_columns(header)
        find = lambda pattern, exclude="^$": next((c for c in columns if re.search(pattern, c["label"], re.I)
                                                    and not re.search(exclude, c["label"], re.I)), None)
        desc = find(r"description|item|product|particular")
        taxable = find(r"taxable", r"^unit")
        total = find(r"\btotal\b", r"sub")
        hsn_col = find(r"^hsn")
        gst_cols = [c for c in columns if GST_COLUMN.search(c["label"]) and not re.search(r"taxable", c["label"], re.I)]
        if not gst_cols or not hsn_col:
            continue

        tops = [c[2] for c in codes]
        gaps = [b - a for a, b in zip(tops, tops[1:])] or [40]
        for i, code in enumerate(codes):
            upper = (tops[i - 1] + tops[i]) / 2 if i else max(f[3] for f in header)
            lower = (tops[i] + tops[i + 1]) / 2 if i + 1 < len(tops) else tops[i] + min(gaps) / 2
            cells = {}
            for f in sorted((f for f in page if upper <= (f[2] + f[3]) / 2 < lower), key=lambda f: (round(f[2]), f[0])):
                cells.setdefault(id(column_of(f, columns, desc)), []).append(f[4])
            gst_rates, gst_amounts = numbers(t for c in gst_cols for t in cells.get(id(c), []))
            first_amount = lambda col: next(iter(numbers(cells.get(id(col), []))[1]), None) if col else None
            row = {
                "label": " ".join(cells.get(id(desc), [])) if desc else code[4],
                "hsn": code[4],
                "gst_rate": round(sum(gst_rates), 2),
                "taxable": first_amount(taxable),
                "tax": round(sum(gst_amounts), 2),
                "amount": first_amount(total),
            }
            if row["amount"] is None and row["taxable"] is not None:
                row["amount"] = round(row["taxable"] + row["tax"], 2)
            rows.append(row)
    return rows


def table_fields(rows):
    """Bill fields for a tax invoice read row by row: every line already includes its GST."""
    items = [r for r in rows if r["amount"] is not None]
    return {"items": items, "fees": [], "subtotal": None, "taxes": [], "discounts": [],
            "total": round(sum(i["amount"] for i in items), 2)}


NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def printed_numbers(text):
    """Every value a number printed in the text can stand for, allowing for two known OCR misreads: the rupee
    sign read as a leading 7, 2 or 1 (₹8.10 -> 78.10) and a thousands comma read as a dot (11,800 -> 11.800)."""
    values = set()
    for token in NUMBER.findall(text):
        for candidate in (token, token[1:] if token[0] in "127" and len(token) > 2 else None):
            if not candidate or not candidate[0].isdigit():
                continue
            values.add(float(candidate.replace(",", "")))
            if re.fullmatch(r"\d{1,3}(\.\d{3})+", candidate):
                values.add(float(candidate.replace(".", "")))
    return values


def to_number(text):
    """An amount as written, keeping a minus sign written before the digits ("$-0.71", "-₹140")."""
    text = str(text)
    digits = re.sub(r"[^\d.,]", "", text).replace(",", "").strip(".")
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if re.match(r"^[^\d]*-", text) else value


def verify_reading(reading, lines):
    """Keep only what the LLM read that is really printed: each amount must appear on the line it cites (or a
    neighbouring line), and each label's words must be there too. Everything else is dropped and recorded."""
    def check(entry, needs_label):
        if not isinstance(entry, dict):
            return None
        try:
            n = int(entry.get("line")) - 1
        except (TypeError, ValueError):
            return None
        nearby = " ".join(lines[max(n - 1, 0):n + 2]) if 0 <= n < len(lines) else ""
        amount = to_number(entry.get("amount"))
        if amount is None or abs(amount) not in printed_numbers(nearby):
            return None
        label = " ".join(str(entry.get("label") or "").split())[:120]
        label_words = re.findall(r"[a-z]{3,}", label.lower())
        if needs_label and (not label or label_words and not any(w in nearby.lower() for w in label_words)):
            return None
        return {"label": label, "amount": amount}

    fields = {"items": [], "fees": [], "discounts": [], "taxes": [], "subtotal": None, "total": None, "dropped": [],
              "read_by": "llm", "rounds_total": any("round" in line.lower() for line in lines)}  # "Round off", "Round sum payable"
    seen = set()
    for kind in ("items", "fees", "discounts", "taxes"):
        for entry in reading.get(kind) or []:
            kept = check(entry, needs_label=True)
            if kept is None:
                fields["dropped"].append({"kind": kind, "entry": str(entry)[:200]})
                continue
            key = (entry.get("line"), kept["amount"])
            if key in seen:  # the same printed amount listed twice
                continue
            seen.add(key)
            if kind == "discounts":
                kept["amount"] = -abs(kept["amount"])
            fields[kind].append(kept)
    for kind in ("subtotal", "total"):
        entry = reading.get(kind)
        if entry:
            kept = check(entry, needs_label=False)
            if kept is None:
                fields["dropped"].append({"kind": kind, "entry": str(entry)[:200]})
            else:
                fields[kind] = kept["amount"]
    return fields


def merge_fields(*all_fields):
    """One set of fields from several readings (typed promises plus a promised file)."""
    merged = {"items": [], "fees": [], "discounts": [], "taxes": [], "subtotal": None, "total": None}
    for fields in all_fields:
        for kind in ("items", "fees", "discounts", "taxes"):
            merged[kind] += fields.get(kind, [])
        for kind in ("subtotal", "total"):
            if merged[kind] is None:
                merged[kind] = fields.get(kind)
    return merged


def parse(lines):
    """Typed promised amounts, one per line, like "Installation: 0"."""
    fields = {"items": [], "fees": [], "subtotal": None, "taxes": [], "discounts": [], "total": None}
    for line in lines:
        match = AMOUNT.search(line)
        if not match:
            continue
        label = re.sub(r"(\s+[\d,.]+)+$", "", line[: match.start()]).strip(" :-=.\t")
        if not re.search(r"[a-z]", label, re.I) or IGNORE.search(label):
            continue
        amount = float(match.group(1).replace(",", ""))
        if SUBTOTAL.search(label):
            fields["subtotal"] = amount
        elif TAX.search(label) and not re.search(r"incl|grand|payable|due", label, re.I):
            fields["taxes"].append({"label": label, "amount": amount})
        elif TOTAL.search(label):
            fields["total"] = amount  # the last total on the bill wins
        elif DISCOUNT.search(label):
            fields["discounts"].append({"label": label, "amount": -abs(amount)})
        else:
            fields["items"].append({"label": label, "amount": amount})
    return fields


def is_empty(fields):
    return not any(fields.get(kind) for kind in ("items", "fees", "discounts")) and fields["total"] is None


def math_findings(fields):
    """Does the bill add up? For a bill read by the LLM a mismatch may be a reading error rather than the bill's,
    so it is reported as check_reading (a question for the user) instead of math_error (a dispute)."""
    kind = "check_reading" if fields.get("read_by") == "llm" else "math_error"
    advice = " The reading may be off: compare with your bill before disputing." if kind == "check_reading" else ""
    findings = []
    items_sum = round(sum(i["amount"] for i in fields["items"]), 2)
    subtotal = fields["subtotal"]
    if subtotal is not None and fields["items"] and abs(items_sum - subtotal) > 0.01:
        findings.append({
            "type": kind, "label": "Subtotal", "billed": subtotal,
            "note": f"Line items add up to {items_sum:.2f}, but the subtotal says {subtotal:.2f}.{advice}",
        })
    total = fields["total"]
    if total is not None and (fields["items"] or subtotal is not None):
        base = subtotal if subtotal is not None else items_sum
        added = sum(x["amount"] for group in ("fees", "taxes", "discounts") for x in fields.get(group, []))
        expected = round(base + added, 2)
        tolerance = 10 if fields.get("rounds_total") else 0.5  # rupee round-off, or a bill that says it rounds
        if abs(expected - total) > tolerance:
            findings.append({
                "type": kind, "label": "Total", "billed": total,
                "note": f"Items, fees, taxes and discounts add up to {expected:.2f}, but the total says {total:.2f}.{advice}",
            })
    return findings


def reading_problems(fields):
    """How far a reading is from adding up: one point per arithmetic mismatch, one for a missing total."""
    return len(math_findings(fields)) + (fields["total"] is None)


def words(text):
    return set(re.findall(r"[a-z]+|\d+", text.lower())) - FILLER


def similarity(a, b):
    wa, wb = words(a), words(b)
    overlap = len(wa & wb) / len(wa | wb) if wa | wb else 0
    return max(overlap, SequenceMatcher(None, a.lower(), b.lower()).ratio())


def finding(kind, label, billed=None, promised=None):
    notes = {
        "match": lambda: f"{label}: {billed:.2f} matches what was promised",
        "mismatch": lambda: f"{label}: billed {billed:.2f}, promised {promised:.2f} ({billed - promised:+.2f})",
        "missing_on_bill": lambda: f"{label}: promised {promised:.2f}, but not found on the bill",
        "extra_on_bill": lambda: f"{label}: billed {billed:.2f}, but not in the promised values",
        "unverified": lambda: f"{label}: billed {billed:.2f}",
    }
    return {"type": kind, "label": label, "billed": billed, "promised": promised, "note": notes[kind]()}


def compare(bill, promised):
    findings = math_findings(bill)
    bill_lines = bill["items"] + bill.get("fees", []) + bill["discounts"]
    if promised is None:
        return findings + [finding("unverified", b["label"], billed=b["amount"]) for b in bill_lines]

    promised_lines = promised["items"] + promised.get("fees", []) + promised["discounts"]
    def score(p, b):
        """Name similarity, plus a bonus when the amounts agree. Lines pair up when their names are similar (0.5),
        or fairly similar (0.3) with the same amount, like "Discount: 140" and "Discount Applied -140"."""
        name = similarity(p["label"], b["label"])
        same_amount = abs(abs(b["amount"]) - abs(p["amount"])) <= 0.01
        return name + (0.3 if same_amount else 0), name >= 0.5 or (same_amount and name >= 0.3)

    pairs = sorted(
        ((*score(p, b), pi, bi) for pi, p in enumerate(promised_lines) for bi, b in enumerate(bill_lines)),
        reverse=True,
    )
    matched = {}
    for _, close_enough, pi, bi in pairs:
        if close_enough and pi not in matched and bi not in matched.values():
            matched[pi] = bi

    for pi, p in enumerate(promised_lines):
        if pi not in matched and p["amount"] == 0:  # promised free and not charged: as promised
            findings.append({"type": "match", "label": p["label"], "billed": None, "promised": 0.0,
                             "note": f"{p['label']}: not charged, as promised"})
            continue
        if pi not in matched:
            findings.append(finding("missing_on_bill", p["label"], promised=p["amount"]))
            continue
        b = bill_lines[matched[pi]]
        kind = "match" if abs(b["amount"] - p["amount"]) <= 0.01 else "mismatch"
        findings.append(finding(kind, b["label"], billed=b["amount"], promised=p["amount"]))
    for bi, b in enumerate(bill_lines):
        if bi not in matched.values() and b["amount"] > 0:  # an unpromised discount is the customer's gain
            findings.append(finding("extra_on_bill", b["label"], billed=b["amount"]))

    if bill["total"] is not None and promised["total"] is not None:
        kind = "match" if abs(bill["total"] - promised["total"]) <= 0.01 else "mismatch"
        findings.append(finding(kind, "Total", billed=bill["total"], promised=promised["total"]))
    return findings
