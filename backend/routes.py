import re
import uuid
from contextlib import suppress

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend import db, extract, gst, llm

router = APIRouter()
MAX_BYTES = 20 * 1024 * 1024
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
        raise HTTPException(413, f"The {name} file must be 20 MB or smaller.")
    mime = extract.file_type(data)
    if not mime:
        raise HTTPException(415, f"The {name} file must be a PDF or a JPEG, PNG or WebP image.")
    return data, mime


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
    bill_fields = extract.table_fields(gst_rows) if gst_rows else extract.parse(bill_lines)
    if extract.is_empty(bill_fields):
        raise HTTPException(422, "Couldn't read any amounts from the bill. Upload a clearer photo or the original PDF.")
    bill_fields["header"] = bill_lines[:3]

    promised_lines = promised_text.splitlines()
    if promised and promised.filename:
        files["promised"] = read_upload(promised, "promised values")
        promised_lines += extract.read_lines(extract.read_pages(*files["promised"]))
    promised_fields = extract.parse(promised_lines) if promised_lines else None
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
