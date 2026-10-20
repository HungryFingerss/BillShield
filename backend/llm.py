"""LLM calls. The LLM may point at amounts printed in a document, but code checks every one of them against the
text; GST rates come only from the official tables, and arithmetic is always done by code."""
import json
import os
import re
import time

import requests
from fastapi import HTTPException

EXPLAIN = """You help a customer dispute a bill. The findings below were computed by code from the bill.
Treat every number as final: do not recalculate, add, or invent amounts, rates, dates or names.
Findings of type "worth_confirming" are not errors: for those, politely ask the provider to confirm the HSN
classification and GST rate, without claiming it is wrong. Findings of type "unverified" are charges the customer
wants explained: ask the provider what the charge is for and why it applies, without claiming it is wrong.
Findings of type "check_reading" mean the bill's amounts may not add up to its total: ask the provider to confirm
how the total is made up, without claiming an error.
Reply with a JSON object with two plain-text fields (no Markdown):
"explanation": a short, plain explanation of each disputed finding for the customer.
"message": a polite, firm message the customer can send, listing each disputed item with its amounts and asking
for a corrected bill or refund (or confirmation or an explanation, for "worth_confirming" and "unverified" items). Use the bill header to name the
provider if it is clear; otherwise write "Dear Customer Support". Sign it with the customer's name."""

CLASSIFY = """You check HSN classification on Indian GST invoices. For each item you get the product as printed on
the bill, the official entries for the HSN code the seller used ("billed_entries"), and candidate official entries
found by searching the official rate table for words in the product name ("candidates").
Decide whether the product fits one of its billed entries. Only when it plainly does not, and one candidate clearly
describes the product, choose that candidate by its id. Choose only from the candidates given; if none clearly
describes the product, answer that it fits as billed. Do not mention GST rates; they are looked up separately.
Reply with JSON: {"items": [{"item": <number>, "fits": true or false, "candidate": <candidate id> or null,
"reason": "<one short sentence about what the product is>"}]}"""

READ = """You read a document a customer uploaded to check a bill. Its text comes from a PDF or from OCR of a photo
or screenshot, as numbered lines; " | " marks a wide gap between columns. A table header's values are usually on the
next line in the same column order, and a label may sit on the line just above its amount. OCR may garble letters
in words (B read as R, P as D) and read the rupee sign as a letter or digit stuck to the amount.
First decide whether it is a bill, invoice, receipt, order bill summary, quote or estimate: something that charges
or quotes amounts to the customer ("is_bill"). Utility bills and loan, EMI or credit card statements with an amount
due count as bills. Bank account statements (a list of the customer's own transactions with running balances),
gazettes, letters, ID documents and menus are not bills.
If it is, list what the customer is charged:
- "items": goods and services as listed, and charges that are part of the goods (one entry per line item)
- "fees": charges added on top of the items: delivery, platform, packaging, convenience, handling, service, late
  fees, previous outstanding when it is part of the amount due (count a fee here even if it includes GST)
- "discounts": reductions, coupons, cashback applied to this bill
- "taxes": tax lines: GST, CGST, SGST, IGST, VAT, cess, "Taxes"
- "subtotal": the printed total of the items before fees, taxes and discounts (Item Total, Sub Total), if printed
- "total": the final amount the customer pays (Bill Total, Grand Total, Amount Payable, Total Amount Due), if printed
Never list dates, years, times, order, invoice, account or reference numbers, PIN codes, phone numbers, quantities,
units, meter readings, chart values, refunds, savings or status lines as amounts. Skip struck-out prices shown as FREE.
When a bill shows the same charges both as a summary and as a detailed breakdown, list them once, at the level that
adds up to the total, and never list a section subtotal (a line that sums the lines above it) as an item.
Skip conditional amounts that only apply if the customer pays early or late (early-payment discounts, late payment
charges, the amount payable after the due date): "total" is the amount due now.
For each amount give the number of the line it is printed on and copy the number exactly as printed. Never compute,
round, convert or invent an amount: if a value is not printed, leave it out.
OCR sometimes reads the rupee sign as a leading 7, 2 or 1 (Rs 2670.00 printed as 22670.00, Rs 8.10 as 78.10) and a
thousands comma as a dot (11,800 as 11.800). Only when the context makes such a misread clear, give the amount
without it (2670.00, 8.10, 11,800).
Reply with JSON: {"is_bill": true or false, "document": "<what this is, in a few words>",
"items": [{"label": "<name as printed>", "amount": "<as printed>", "line": <line number>}], "fees": [...],
"discounts": [...], "taxes": [...], "subtotal": {"amount": "...", "line": <n>} or null,
"total": {"amount": "...", "line": <n>} or null}"""
MAX_CHARS = 12000  # text sent for reading; longer documents are cut so the request fits the free tier
TOKEN_LIMIT = 7600  # Groq's free tier refuses any request over 8,000 tokens (prompt plus answer); keep a margin
CHARS_PER_TOKEN = 3  # measured about 3.4 on a long bill; 3 errs on the side of more tokens
MEDIUM_UP_TO = 2500  # prompts up to this many tokens get medium reasoning; longer ones low, which thinks less
TOO_LONG = "This document is too long to read in one go. Upload only the pages that show the charges."


def post(body):
    """POST to Groq, waiting and retrying when its per-minute token limit is hit (the free tier allows 8,000)."""
    for attempt in range(3):
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            json=body,
            timeout=60,
        )
        if response.status_code != 429 or attempt == 2:
            return response
        time.sleep(min(float(response.headers.get("retry-after") or 10), 30))


def ask(instructions, facts, failure, effort="low", temperature=0.2, max_tokens=None):
    """One JSON-mode chat call. Any failure (no key, network, bad reply) becomes a 502 with the given message;
    a request too large for Groq's per-request limit (413) becomes a 413 saying the document is too long."""
    try:
        response = post({
            "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "reasoning_effort": effort,
            **({"max_completion_tokens": max_tokens} if max_tokens else {}),
        })
        if response.status_code == 413:
            raise HTTPException(413, TOO_LONG)
        response.raise_for_status()
        reply = json.loads(response.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError, requests.RequestException):
        raise HTTPException(502, failure) from None
    if not isinstance(reply, dict):
        raise HTTPException(502, failure)
    return reply


def explain(customer_name, bill, disputed):
    failure = "Couldn't write the message right now. Try again."
    reply = ask(EXPLAIN, {
        "customer_name": customer_name,
        "bill_header": bill.get("header", []),
        "bill_total": bill["total"],
        "disputed_findings": [{"type": f["type"], "finding": f["note"]} for f in disputed],
    }, failure)
    explanation, message = reply.get("explanation"), reply.get("message")
    if not isinstance(explanation, str) or not isinstance(message, str):
        raise HTTPException(502, failure)
    return explanation.strip(), message.strip()


def worth_reading(line):
    """Lines that can hold an amount or a label: anything with a digit, a column mark, or short enough to be a
    label. Long paragraphs without numbers (adverts, safety notes, definitions) are left out."""
    return bool(re.search(r"\d", line)) or " | " in line or len(line) <= 60


def reading_text(lines):
    """The numbered text sent for reading. Lines keep their original numbers, so the LLM's line references still
    point at the right text."""
    return "\n".join(f"{n}: {line}" for n, line in enumerate(lines, 1) if worth_reading(line))[:MAX_CHARS]


def is_long(lines):
    """A document whose reading needs more than MEDIUM_UP_TO prompt tokens."""
    return (len(READ) + len(reading_text(lines))) // CHARS_PER_TOKEN > MEDIUM_UP_TO


def read_document(lines):
    """The LLM's reading of a document's charges. Unchecked: extract.verify_reading decides what is kept.
    Long documents get low reasoning, and the answer is capped so the request stays under Groq's per-request
    limit."""
    numbered = reading_text(lines)
    prompt_tokens = (len(READ) + len(numbered)) // CHARS_PER_TOKEN
    effort = "medium" if prompt_tokens <= MEDIUM_UP_TO else "low"
    return ask(READ, {"lines": numbered}, "Couldn't read the bill right now. Try again.", effort=effort,
               temperature=0, max_tokens=TOKEN_LIMIT - prompt_tokens)


def classify_hsn(items):
    answers = ask(CLASSIFY, {"items": items}, "Couldn't check HSN classification.").get("items")
    return [a for a in answers if isinstance(a, dict)] if isinstance(answers, list) else []
