"""LLM calls. They only give reasoning and wording: every amount and rate comes from code or the official tables."""
import json
import os

import requests
from fastapi import HTTPException

EXPLAIN = """You help a customer dispute a bill. The findings below were computed by code from the bill.
Treat every number as final: do not recalculate, add, or invent amounts, rates, dates or names.
Findings of type "worth_confirming" are not errors: for those, politely ask the provider to confirm the HSN
classification and GST rate, without claiming it is wrong.
Reply with a JSON object with two plain-text fields (no Markdown):
"explanation": a short, plain explanation of each disputed finding for the customer.
"message": a polite, firm message the customer can send, listing each disputed item with its amounts and asking
for a corrected bill or refund (or confirmation, for "worth_confirming" items). Use the bill header to name the
provider if it is clear; otherwise write "Dear Customer Support". Sign it with the customer's name."""

CLASSIFY = """You check HSN classification on Indian GST invoices. For each item you get the product as printed on
the bill, the official entries for the HSN code the seller used ("billed_entries"), and candidate official entries
found by searching the official rate table for words in the product name ("candidates").
Decide whether the product fits one of its billed entries. Only when it plainly does not, and one candidate clearly
describes the product, choose that candidate by its id. Choose only from the candidates given; if none clearly
describes the product, answer that it fits as billed. Do not mention GST rates; they are looked up separately.
Reply with JSON: {"items": [{"item": <number>, "fits": true or false, "candidate": <candidate id> or null,
"reason": "<one short sentence about what the product is>"}]}"""


def ask(instructions, facts, failure):
    """One JSON-mode chat call. Any failure (no key, network, bad reply) becomes a 502 with the given message."""
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            json={
                "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=60,
        )
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


def classify_hsn(items):
    answers = ask(CLASSIFY, {"items": items}, "Couldn't check HSN classification.").get("items")
    return [a for a in answers if isinstance(a, dict)] if isinstance(answers, list) else []
