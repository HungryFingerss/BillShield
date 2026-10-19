"""Supabase Storage and Postgres over plain REST with the server-only secret key."""
import os

import requests
from fastapi import HTTPException

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SECRET_KEY"]
HEADERS = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
BUCKET = "bill-documents"
RATE_FIELDS = "id,hsn,excludes,description,gst_rate,notification,schedule,serial"


def call(method, path, headers=None, **kwargs):
    try:
        response = requests.request(method, URL + path, headers={**HEADERS, **(headers or {})}, timeout=30, **kwargs)
    except requests.RequestException:
        response = None
    if response is None or not response.ok:
        raise HTTPException(502, "Database request failed. Try again.")
    return response.json() if response.content else None


def save_user(name, email):
    """Create the user, or update their name if the email already exists."""
    return call(
        "POST", "/rest/v1/users", params={"on_conflict": "email"}, json={"name": name, "email": email},
        headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )[0]["id"]


def hsn_rates(codes):
    """Official rate entries whose HSN is any prefix of the given codes."""
    prefixes = sorted({code[:n] for code in codes for n in range(2, len(code) + 1)})
    return call("GET", "/rest/v1/hsn_rates", params={"hsn": f"in.({','.join(prefixes)})", "select": RATE_FIELDS})


def search_hsn_rates(words):
    """Official entries whose description best matches any of the words (Postgres full-text search)."""
    return call("POST", "/rest/v1/rpc/search_hsn_rates", params={"select": RATE_FIELDS},
                json={"words": words, "max_rows": 15})


def upload(path, data, mime):
    call("POST", f"/storage/v1/object/{BUCKET}/{path}", headers={"Content-Type": mime}, data=data)


def remove(paths):
    call("DELETE", f"/storage/v1/object/{BUCKET}", json={"prefixes": paths})


def insert_check(row):
    call("POST", "/rest/v1/checks", json=row)


def get_check(user_id, check_id):
    rows = call("GET", "/rest/v1/checks", params={"id": f"eq.{check_id}", "user_id": f"eq.{user_id}", "select": "*,users(name)"})
    if not rows:
        raise HTTPException(404, "Check not found.")
    return rows[0]


def update_check(check_id, values):
    call("PATCH", "/rest/v1/checks", params={"id": f"eq.{check_id}"}, json=values)
