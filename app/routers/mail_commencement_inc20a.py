from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
import json
import ssl
import smtplib
from email.message import EmailMessage
from typing import List, Optional

from ..settings import (
    COMPANIES_ROOT,
    EMAILS_ROOT,
    SMTP_SERVER,
    SMTP_PORT,
    SENDER_EMAIL,
    SENDER_PASSWORD,
)

router = APIRouter(
    prefix="/api/mail",
    tags=["mail-commencement-inc20a"],
)


class CommencementMailRequest(BaseModel):
    # company folder name exactly as it appears under /companies
    company_key: str


def _find_latest_inc20a_doc(company_dir: Path) -> Optional[Path]:
    """
    Find latest file matching:
      bank_certificate_inc20a_*.docx
    directly under the company folder.
    """
    pattern = "bank_certificate_inc20a_*.docx"
    candidates = list(company_dir.glob(pattern))
    print(f"[commencement_inc20a] Searching '{pattern}' in {company_dir}")

    if not candidates:
        print("[commencement_inc20a] No matching INC-20A DOCX found.")
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest = candidates[0]
    print(f"[commencement_inc20a] Using latest: {latest}")
    return latest


def _load_email_template(filename: str) -> str:
    """
    Load HTML template from frontend/emails/<filename>.
    """
    path = EMAILS_ROOT / filename
    print(f"[commencement_inc20a] Loading email template: {path}")
    if not path.exists():
        raise FileNotFoundError(f"Email template not found: {path}")
    return path.read_text(encoding="utf-8")


def _render_template(raw_html: str, context: dict) -> str:
    """
    Very simple {{ placeholder }} replacement.
    Works with both {{ key }} and {{key}}.
    """
    html = raw_html
    for key, value in context.items():
        value_str = "" if value is None else str(value)
        html = html.replace(f"{{{{ {key} }}}}", value_str)
        html = html.replace(f"{{{{{key}}}}}", value_str)
    return html


def _send_mail_with_attachments(
    subject: str,
    html_body: str,
    to: List[str],
    attachments: List[Path],
):
    print(f"[commencement_inc20a] Preparing email to {to}, subject='{subject}'")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(to)

    # Plain-text fallback + HTML alternative
    msg.set_content("This is an HTML email. Please view it in an HTML-capable email client.")
    msg.add_alternative(html_body, subtype="html")

    for p in attachments:
        if not p.exists():
            print(f"[commencement_inc20a] WARNING: attachment not found: {p}")
            continue
        data = p.read_bytes()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=p.name,
        )
        print(f"[commencement_inc20a] Attached file: {p.name}")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)

    print("[commencement_inc20a] Email sent successfully.")
def _find_master_json(company_dir: Path) -> Optional[Path]:
    """
    Look for master JSON files in the company folder.
    Priority:
      1) <company_dir>/_master_*.json
      2) <company_dir>/outputs/_master_*.json
      3) <company_dir>/outputs/master.json  (legacy)
    Returns the latest by modified time, or None if nothing found.
    """
    candidates: list[Path] = []

    # 1) New pattern: directly under company folder
    candidates += list(company_dir.glob("_master_*.json"))

    # 2) Fallback inside outputs/
    outputs_dir = company_dir / "outputs"
    if outputs_dir.exists():
        candidates += list(outputs_dir.glob("_master_*.json"))
        legacy = outputs_dir / "master.json"
        if legacy.exists():
            candidates.append(legacy)

    print(f"[commencement_inc20a] Searching for master JSON in {company_dir}")
    for c in candidates:
        print(f"[commencement_inc20a]  candidate: {c}")

    if not candidates:
        return None

    # pick latest
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest = candidates[0]
    print(f"[commencement_inc20a] Using master JSON: {latest}")
    return latest
@router.post("/preview/commencement-inc20a")
def preview_commencement_inc20a(payload: CommencementMailRequest):
    company_key = payload.company_key
    company_dir = COMPANIES_ROOT / company_key

    print(f"[commencement_inc20a] Preview requested for company_key='{company_key}'")

    if not company_dir.exists():
        msg = f"Company folder not found: {company_dir}"
        print(f"[commencement_inc20a] ERROR: {msg}")
        raise HTTPException(status_code=404, detail=msg)

    master_json = _find_master_json(company_dir)
    if master_json is None:
        msg = f"Master JSON not found in: {company_dir}"
        print(f"[commencement_inc20a] ERROR: {msg}")
        raise HTTPException(status_code=404, detail=msg)

    data = json.loads(master_json.read_text(encoding="utf-8"))

    client_email = (
        data.get("client_email")
        or data.get("email")
        or data.get("client", {}).get("email")
    )
    company_name = (
        data.get("company_name")
        or data.get("company", {}).get("name")
        or company_key
    )

    latest_doc = _find_latest_inc20a_doc(company_dir)
    if latest_doc is None:
        raise HTTPException(
            status_code=404,
            detail="No bank_certificate_inc20a_*.docx file found in company folder.",
        )

    raw_html = _load_email_template("email_commencement_inc20a.html")
    html_body = _render_template(
        raw_html,
        {
            "company_name": company_name,
            "bank_doc_name": latest_doc.name,
        },
    )

    return {
        "ok": True,
        "to": client_email,
        "html": html_body,
    }
def _find_latest_inc20a_doc(company_dir: Path) -> Optional[Path]:
    """
    Find the INC-20A Board Resolution DOCX to attach.
    Only picks:
      - inc20a__*__br_commencement*.docx
      - br_commencement*.docx
    (No bank_certificate_inc20a fallback)
    """
    patterns = [
        "inc20a__*__br_commencement*.docx",  # new generated files
        "br_commencement*.docx",             # older/manual files, if any
    ]

    print(f"[commencement_inc20a] Searching INC-20A BR docs in {company_dir}")

    for pattern in patterns:
        candidates = list(company_dir.glob(pattern))
        print(f"[commencement_inc20a]  pattern '{pattern}' → {len(candidates)} file(s)")
        if not candidates:
            continue
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        latest = candidates[0]
        print(f"[commencement_inc20a]  using latest: {latest}")
        return latest

    print("[commencement_inc20a] No INC-20A BR DOCX found.")
    return None


@router.post("/send/commencement-inc20a")
def send_commencement_inc20a(payload: CommencementMailRequest):
    company_key = payload.company_key
    company_dir = COMPANIES_ROOT / company_key

    print(f"[commencement_inc20a] Triggered for company_key='{company_key}'")

    if not company_dir.exists():
        msg = f"Company folder not found: {company_dir}"
        print(f"[commencement_inc20a] ERROR: {msg}")
        raise HTTPException(status_code=404, detail=msg)

    # 1) Load master.json (adjust path/keys if your structure is different)
    # 1) Load master JSON (new pattern: _master_*.json in company folder)
    master_json = _find_master_json(company_dir)
    if master_json is None:
        msg = f"Master JSON not found in: {company_dir}"
        print(f"[commencement_inc20a] ERROR: {msg}")
        raise HTTPException(status_code=404, detail=msg)

    data = json.loads(master_json.read_text(encoding="utf-8"))



    # Adjust these key names to match your actual master.json
    client_email = (
        data.get("client_email")
        or data.get("email")
        or data.get("client", {}).get("email")
    )
    company_name = (
        data.get("company_name")
        or data.get("company", {}).get("name")
        or company_key
    )

    if not client_email:
        msg = "Client email not found in master.json"
        print(f"[commencement_inc20a] ERROR: {msg}")
        raise HTTPException(status_code=400, detail=msg)

    # 2) Find latest INC-20A bank certificate / resolution DOCX
    latest_doc = _find_latest_inc20a_doc(company_dir)
    if latest_doc is None:
        raise HTTPException(
            status_code=404,
            detail="No bank_certificate_inc20a_*.docx file found in company folder.",
        )

    # 3) Load + render email template
    raw_html = _load_email_template("email_commencement_inc20a.html")
    html_body = _render_template(
        raw_html,
        {
            "company_name": company_name,
            "bank_doc_name": latest_doc.name,
        },
    )

    subject = f"Commencement of Business (INC-20A) – {company_name}"

    # 4) Send mail
    _send_mail_with_attachments(
        subject=subject,
        html_body=html_body,
        to=[client_email],
        attachments=[latest_doc],
    )

    return {
        "ok": True,
        "to": client_email,
        "attached": latest_doc.name,
    }
