"""GST checks on invoice lines that carry an HSN code, against the official rates in the hsn_rates table."""
import re

from fastapi import HTTPException

from backend import db, llm

# 9/2025-Central Tax (Rate), Schedule II S.No. 639: goods not specified in any other schedule are taxed at 18%.
RESIDUAL_RATE = 18.0
NOT_SEARCHED = {"pack", "pouch", "pcs", "piece", "pieces", "combo", "free", "with", "and", "the", "for", "new",
                "bottle", "jar", "box", "carton", "refill", "ltr", "litre", "gms", "kgs", "size", "value"}


def entries_for(code, table):
    return [e for e in table if code.startswith(e["hsn"]) and not any(code.startswith(x) for x in e["excludes"])]


def cite(entry):
    schedule = "" if entry["schedule"] == "Exempt" else f", Schedule {entry['schedule']}"
    return f"Notification {entry['notification']}{schedule}, S.No. {entry['serial']}"


def percent(rate):
    return f"{float(rate):g}%"


def official(entries):
    """Each entry as "5% for <description> (<citation>)", the description cut at a word boundary."""
    short = lambda text: text if len(text) <= 100 else text[:100].rsplit(" ", 1)[0] + "…"
    return "; ".join(f"{percent(e['gst_rate'])} for {short(e['description'])} ({cite(e)})" for e in entries)


def finding(kind, item, note):
    return {"type": kind, "label": item["label"], "billed": item["gst_rate"], "promised": None, "note": note}


def findings(items):
    rows = [i for i in items if i.get("hsn")]
    if not rows:
        return []
    table = db.hsn_rates({r["hsn"] for r in rows})
    result = []
    for item in rows:
        entries = entries_for(item["hsn"], table)
        allowed = {float(e["gst_rate"]) for e in entries} | {RESIDUAL_RATE}
        if item["gst_rate"] not in allowed:
            result.append(finding("gst_rate_mismatch", item,
                f"{item['label']}: charged {percent(item['gst_rate'])} GST under HSN {item['hsn']}, which is not an "
                f"official rate for that code. Official rates: {official(entries) or 'none listed'}; "
                f"{percent(RESIDUAL_RATE)} for goods not listed elsewhere."))
        if item["taxable"] is not None and abs(item["taxable"] * item["gst_rate"] / 100 - item["tax"]) > 0.05:
            expected = round(item["taxable"] * item["gst_rate"] / 100, 2)
            result.append(finding("math_error", item,
                f"{item['label']}: {percent(item['gst_rate'])} of {item['taxable']:.2f} is {expected:.2f}, "
                f"but the bill charges {item['tax']:.2f} GST"))
    return result + classification_findings(rows, table)


def search_words(label):
    """Words of a product name worth searching the official descriptions for (no units, packaging or short words)."""
    return [w for w in dict.fromkeys(re.findall(r"[a-z]{3,}", label.lower())) if w not in NOT_SEARCHED][:8]


def classification_findings(rows, table):
    """Search the official table for entries matching each product, and let the LLM pick one only from that shortlist."""
    asks, shortlists = [], []
    for i, row in enumerate(rows):
        billed = entries_for(row["hsn"], table)
        billed_ids = {e["id"] for e in billed}
        words = search_words(row["label"])
        candidates = [c for c in db.search_hsn_rates(words) if c["id"] not in billed_ids] if words else []
        shortlists.append({c["id"]: c for c in candidates})
        asks.append({
            "item": i, "product": row["label"], "billed_hsn": row["hsn"],
            "billed_entries": [e["description"][:300] for e in billed],
            "candidates": [{"id": c["id"], "hsn": c["hsn"], "description": c["description"][:300]} for c in candidates],
        })
    try:
        answers = llm.classify_hsn(asks)
    except HTTPException:
        return []  # the rate and math checks above still stand

    result = []
    for answer in answers:
        i = answer.get("item")
        if not isinstance(i, int) or not 0 <= i < len(rows) or answer.get("fits"):
            continue
        try:
            entry = shortlists[i].get(int(answer.get("candidate")))
        except (TypeError, ValueError):
            continue
        item = rows[i]
        if not entry or float(entry["gst_rate"]) == item["gst_rate"]:
            continue  # nothing chosen from the shortlist, or the money would not change
        reason = str(answer.get("reason") or "").strip().rstrip(".")
        result.append(finding("worth_confirming", item,
            f"{item['label']}: billed under HSN {item['hsn']} at {percent(item['gst_rate'])}. The official entry "
            f"that may fit better is HSN {entry['hsn']}: {official([entry])}." + (f" {reason}." if reason else "")))
    return result
