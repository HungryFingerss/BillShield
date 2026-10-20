import re
import uuid
from contextlib import suppress

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend import db, extract, gst, llm

router = APIRouter()
MAX_BYTES = 4 * 1024 * 1024  # Vercel accepts request bodies up to 4.5 MB; the page shrinks photos to fit
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class User(BaseModel):
    name: str
    email: str


class Decision(BaseModel):
    user_id: uuid.UUID
    disputed: list[int]


def read_upload(upload, name):
    data = upload.file.read(MAX_BYTES + 1)
    if not data:
        raise HTTPException(400, f"The {name} file is empty.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"The {name} file must be 4 MB or smaller.")
    mime = extract.file_type(data)
    if not mime:
        raise HTTPException(415, f"The {name} file must be a PDF or a JPEG, PNG or WebP image.")
    return data, mime


def read_bill(lines, attempts=3):
    """Let the LLM read the bill and keep only what code can verify. A reading that adds up to its total is taken
    at once; otherwise the bill is read again, and the reading closest to adding up wins."""
    best = None
    for _ in range(attempts):
        reading = llm.read_document(lines)
        if reading.get("is_bill") is False:
            document = " ".join(str(reading.get("document") or "a document").split())[:80]
            raise HTTPException(422, f"This looks like {document}, not a bill. Upload a bill, invoice or receipt.")
        fields = extract.verify_reading(reading, lines)
        problems = extract.reading_problems(fields)
        if best is None or problems < best[0]:
            best = (problems, fields)
        if problems == 0:
            break
    return best[1]


@router.post("/users")
def save_user(user: User):
    name, email = user.name.strip(), user.email.strip().lower()
    if not name:
        raise HTTPException(400, "Enter your name.")
    if not EMAIL.match(email):
        raise HTTPException(400, "Enter a valid email address.")
    return {"user_id": db.save_user(name, email)}


@router.post("/checks")
def create_check(
    user_id: uuid.UUID = Form(...),
    bill: UploadFile = File(...),
    promised: UploadFile | None = File(None),
    promised_text: str = Form(""),
):
    files = {"bill": read_upload(bill, "bill")}
    pages = extract.read_pages(*files["bill"])
    bill_lines = extract.read_lines(pages)
    gst_rows = extract.gst_rows(pages)
    bill_fields = extract.table_fields(gst_rows) if gst_rows else read_bill(bill_lines)
    if extract.is_empty(bill_fields):
        raise HTTPException(422, "Couldn't read any amounts from the bill. Upload a clearer photo or the original PDF.")
    bill_fields["header"] = bill_lines[:3]
    bill_fields["low_resolution"] = extract.is_low_resolution(*files["bill"])

    promised_parts = [extract.parse(promised_text.splitlines())] if promised_text.strip() else []
    if promised and promised.filename:
        files["promised"] = read_upload(promised, "promised values")
        promised_file_lines = extract.read_lines(extract.read_pages(*files["promised"]))
        promised_parts.append(extract.verify_reading(llm.read_document(promised_file_lines), promised_file_lines))
    promised_fields = extract.merge_fields(*promised_parts) if promised_parts else None
    if promised_fields and extract.is_empty(promised_fields):
        raise HTTPException(422, "Couldn't read any amounts from the promised values. Write one per line, like 'Installation: 0'.")

    findings = extract.compare(bill_fields, promised_fields) + gst.findings(bill_fields["items"])
    check_id = str(uuid.uuid4())
    paths = {name: f"{user_id}/{check_id}/{name}.{mime.split('/')[1]}" for name, (_, mime) in files.items()}
    uploaded = []
    try:
        for name, (data, mime) in files.items():
            db.upload(paths[name], data, mime)
            uploaded.append(paths[name])
        db.insert_check({
            "id": check_id,
            "user_id": str(user_id),
            "bill_path": paths["bill"],
            "promised_path": paths.get("promised"),
            "promised_text": promised_text or None,
            "bill": bill_fields,
            "promised": promised_fields,
            "findings": findings,
        })
    except Exception:
        with suppress(HTTPException):
            db.remove(uploaded)
        raise
    return {"id": check_id, "bill": bill_fields, "promised": promised_fields, "findings": findings}


@router.post("/checks/{check_id}/message")
def write_message(check_id: uuid.UUID, decision: Decision):
    check = db.get_check(decision.user_id, check_id)
    findings = check["findings"]
    disputed = sorted({i for i in decision.disputed if 0 <= i < len(findings) and findings[i]["type"] != "match"})
    if disputed:
        explanation, message = llm.explain(check["users"]["name"], check["bill"], [findings[i] for i in disputed])
    else:
        explanation, message = "Nothing was disputed, so there's no message to send.", ""
    db.update_check(check_id, {"disputed": disputed, "explanation": explanation, "message": message, "status": "done"})
    return {"explanation": explanation, "message": message}
