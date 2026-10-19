# BillShield

Check a bill against what you were promised, and against India's official GST rates. BillShield reads the bill with
code, shows every difference it finds, lets you decide what to dispute, and drafts the message to the provider.

The LLM never reads the bill and never produces a number: every amount comes from the bill, and every GST rate from
the official CBIC notifications.

## What it checks

| Check | How |
|---|---|
| Promised vs billed | Each promised line is matched to a bill line by name; different, missing and unpromised amounts are flagged |
| The bill's own math | Line items vs subtotal, subtotal + taxes − discounts vs total |
| GST rate per line | For invoices with an HSN column: the billed rate must be an official rate for that HSN code |
| GST amount per line | Taxable value × rate must equal the tax charged |
| HSN classification | Code searches the official rate table for the product; the LLM may pick a better-fitting entry from that shortlist, and the app suggests asking the seller when its official rate differs |

Example: a BigBasket invoice billed "Finish Dishwasher Salt" under HSN 3402 at 18%. BillShield finds HSN 2501
(salt), exempt at 0% under Notification 10/2025-Central Tax (Rate), S.No. 109, and drafts a polite question to
the seller.

## How it works

```
Browser (frontend/index.html)
   │  POST /api/users · POST /api/checks · POST /api/checks/{id}/message
   ▼
FastAPI server (backend/)
   ├─ extract.py  read PDF text or OCR a photo, find amounts and GST table rows, compare   (no AI)
   ├─ gst.py      official rate lookup, tax math, HSN shortlist                           (no AI)
   ├─ llm.py      Groq: pick from the shortlist; write the explanation and message
   └─ db.py       Supabase: users, checks, hsn_rates, bill files
```

1. **Identify:** name and email, no password. The email is the user's identity.
2. **Check:** upload a bill (PDF or photo), optionally with a quote or typed promised amounts. The server reads it,
   runs every check, saves the files and the findings, and returns them.
3. **Decide:** each finding is a checkbox. Untick what you agreed to; tick what to dispute or ask about.
4. **Message:** the LLM explains the ticked findings and writes the message; copy it and send it.

## Project layout

```
backend/
  __init__.py   loads .env
  main.py       FastAPI app; serves the page and mounts /api
  routes.py     the three /api endpoints
  extract.py    reading bills, parsing amounts and GST tables, comparisons
  gst.py        GST rate, tax math and HSN shortlist checks
  llm.py        the two Groq calls
  db.py         Supabase REST and Storage calls
frontend/
  index.html    the whole UI: one page, no framework
tools/
  load_hsn_rates.py   builds the hsn_rates table from the notification PDFs
data/notifications/   official CBIC GST rate notifications (PDF)
schema.sql            the full database: tables, indexes, search function, bucket
```

## Setup

Requires Python 3.12+, a Supabase project and a Groq API key.

1. Install the packages:
   ```
   pip install -r requirements.txt
   ```
2. Create the database: run `schema.sql` in the Supabase SQL editor.
3. Copy `.env.example` to `.env` and fill it in:

   | Variable | What it is |
   |---|---|
   | `SUPABASE_URL` | Your project URL, `https://<project-ref>.supabase.co` |
   | `SUPABASE_SECRET_KEY` | The project's secret key. Server only: it bypasses row-level security |
   | `GROQ_API_KEY` | Groq API key |
   | `GROQ_MODEL` | Optional, defaults to `openai/gpt-oss-120b` |

4. Load the official GST rates (about 1,500 rows):
   ```
   python -m tools.load_hsn_rates
   ```
5. Start the server and open http://127.0.0.1:8000:
   ```
   python -m uvicorn backend.main:app --reload
   ```

## API

| Endpoint | Request | Response |
|---|---|---|
| `POST /api/users` | JSON `{name, email}` | `{user_id}` |
| `POST /api/checks` | Form: `user_id`, `bill` file, optional `promised` file and `promised_text` | `{id, bill, promised, findings}` |
| `POST /api/checks/{id}/message` | JSON `{user_id, disputed: [positions of ticked findings]}` | `{explanation, message}` |
| `GET /health` | | `{status: "ok"}` |

## Security

- Secrets live only in `.env` on the server; the browser talks only to the FastAPI server.
- Row-level security is on for every table with no policies, so Supabase's public keys can read nothing.
- Uploaded files go to a private bucket under `<user_id>/<check_id>/`.
- There is no login: anyone who types an email acts as that user. Add email verification before showing past checks.

## GST data

`hsn_rates` comes from these CBIC notifications, each row keeping its notification, schedule and serial number:

| Notification | Contents | In force from |
|---|---|---|
| 9/2025-Central Tax (Rate) | Rate schedules (5%, 18%, 40%, 3%, 0.25%, 1.5%) | 22 Sep 2025 |
| 10/2025-Central Tax (Rate) | Exempt goods (0%) | 22 Sep 2025 |
| 19/2025-Central Tax (Rate) | Biris 18%, tobacco 40%, 28% schedule removed | 1 Feb 2026 |
| 01/2026-Central Tax (Rate) | Four beverage HSN entries | 1 May 2026 |

When CBIC amends rates, add the change to `AMENDMENTS` in `tools/load_hsn_rates.py` and run the loader again.

## Limits

- Bills without an HSN table are read by amounts at the end of each line; unusual layouts may need more rules.
- A GST table must have its header row (with HSN and GST columns) on each page it appears on.
- Matching promised to billed lines is by wording: "Internet plan" won't match "Broadband 100 Mbps".
- The HSN shortlist needs words shared between the product name and the official description.
