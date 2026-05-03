# app/routers/mail.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import json, mimetypes, ssl, smtplib, os
from email.message import EmailMessage
import pandas as pd
from jinja2 import Template

from ..settings import COMPANIES_ROOT  # required

# Optional in settings.py (recommended). If missing, we'll fallback.
try:
    from ..settings import FRONTEND_ROOT as _FRONTEND_ROOT
except Exception:
    _FRONTEND_ROOT = None

try:
    from ..settings import AUDITORS_MASTER_PATH as _AUDITORS_MASTER_PATH
except Exception:
    _AUDITORS_MASTER_PATH = None

router = APIRouter(prefix="/api/mail", tags=["mail"])

# =========================
# SMTP / Gmail (SSL: 465)
# =========================
# NOTE: You asked to hard-wire these. Safer to use env vars in production.
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT   = 465
SENDER_EMAIL = "vinayakkonar@virtualfinanceservice.com"
SENDER_PASSWORD = "kxpzif sfrxiap keg".replace(" ", "")  # ensure no spaces

# =========================
# Locations
# =========================
BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
FRONTEND_ROOT = _FRONTEND_ROOT if _FRONTEND_ROOT else DEFAULT_FRONTEND_ROOT

DEFAULT_AUDITORS_MASTER = (BACKEND_ROOT / "templates" / "auditors_master_final.xlsx")
AUDITORS_MASTER_PATH = Path(_AUDITORS_MASTER_PATH) if _AUDITORS_MASTER_PATH else DEFAULT_AUDITORS_MASTER

# =========================
# Request bodies
# =========================
class SendAuditorBody(BaseModel):
    company_name: str
    auditor_id: str

class SendCompanyBody(BaseModel):
    company_name: str

# =========================
# Helpers: files & context
# =========================
DOC_PREFIXES = {
    "bod": "bod_auditor_first_appointment",
    "intimation": "intimation_first_auditor_TEMPLATE",
    "proposal": "proposal_first_auditor_TEMPLATE",
    "consent": "auditor_consent_TEMPLATE",
}

def _latest_docs(company_name: str) -> Dict[str, Path]:
    folder = COMPANIES_ROOT / company_name
    out: Dict[str, Path] = {}
    if not folder.exists():
        return out
    for key, prefix in DOC_PREFIXES.items():
        matches = sorted(folder.glob(f"{prefix}_*.docx"))
        if matches:
            out[key] = matches[-1]
    return out

def _dl_url(p: Path) -> str:
    # relies on app.mount("/companies", StaticFiles(...)) in main.py
    rel = p.relative_to(COMPANIES_ROOT).as_posix()
    return f"/companies/{rel}"

def _render_html_template(rel_path: str, ctx: dict) -> str:
    tpl_path = (FRONTEND_ROOT / rel_path)
    if not tpl_path.exists():
        raise FileNotFoundError(f"Email template not found: {tpl_path}")
    raw = tpl_path.read_text(encoding="utf-8")
    return Template(raw).render(**ctx)

def _safe_master(company_name: str) -> Tuple[Optional[Path], dict]:
    """Return (path, data) for latest _master_*.json under company folder."""
    folder = COMPANIES_ROOT / company_name
    if not folder.exists():
        return (None, {})
    masters = sorted(folder.glob("_master_*.json"))
    if not masters:
        return (None, {})
    mp = masters[-1]
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
        return (mp, data if isinstance(data, dict) else {})
    except Exception:
        return (mp, {})

def _company_ctx_from_master(master: dict, fallback_name: str) -> dict:
    md = master or {}
    # Allow both nested structures or flat (we keep it simple)
    company_email = md.get("email") or md.get("company_email") or "-"
    return {
        "name": md.get("company_name") or fallback_name,
        "cin": md.get("cin") or "-",
        "pan": md.get("pan") or "-",  # if nested structure, adapt accordingly
        "tan": md.get("tan") or "-",
        "email": company_email,
    }

def _directors_emails_from_master(master: dict) -> List[str]:
    emails: List[str] = []
    dirs = (master or {}).get("directors") or []
    for row in dirs:
        e = (row or {}).get("email")
        if e and isinstance(e, str):
            e = e.strip()
            if e:
                emails.append(e)
    # Fallback: try company email if no directors have emails
    comp_email = (master or {}).get("email") or (master or {}).get("company_email") or None
    if not emails and comp_email and isinstance(comp_email, str) and comp_email.strip():
        emails = [comp_email.strip()]
    # Deduplicate
    return sorted(list({e.lower(): e for e in emails}.values()))

# NEW: read name + email (+ membership_no) from auditors master
def _auditor_contact_by_id(auditor_id: str) -> Optional[dict]:
    try:
        df = pd.read_excel(AUDITORS_MASTER_PATH)
        # expected columns: auditor_id, auditor_name, membership_no, email
        row = df.loc[df["auditor_id"].astype(str) == str(auditor_id)]
        if row.empty:
            return None
        r = row.iloc[0]
        name = (str(r.get("auditor_name") or "").strip()) or None
        email = (str(r.get("email") or "").strip()) or None
        memno = (str(r.get("membership_no") or "").strip()) or None
        if not email:
            return None
        return {"name": name, "email": email, "membership_no": memno}
    except Exception as e:
        print("[mail] WARN: auditors master lookup failed:", e)
        return None


# =========================
# SMTP send
# =========================
def _send_email_smtp_ssl(to_list: List[str], subject: str, html_body: str, attachments: List[Path]) -> Dict:
    if not (SMTP_SERVER and SMTP_PORT and SENDER_EMAIL and SENDER_PASSWORD):
        return {"sent": False, "reason": "SMTP not configured"}

    if not to_list:
        return {"sent": False, "reason": "No recipients"}

    msg = EmailMessage()
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.set_content("This is an HTML email. Please view it in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    for p in attachments or []:
        try:
            ctype, _ = mimetypes.guess_type(p.name)
            maintype, subtype = (ctype.split("/", 1) if ctype else ("application", "octet-stream"))
            with open(p, "rb") as f:
                msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=p.name)
        except Exception as e:
            print(f"[mail] attach failed: {p} -> {e}")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        return {"sent": True}
    except Exception as e:
        print("[mail] SMTP send failed:", e)
        return {"sent": False, "reason": str(e)}

# =========================
# Endpoints
# =========================
@router.post("/send/auditor")
def send_to_auditor(body: SendAuditorBody):
    print(f"[mail] auditor: start company='{body.company_name}' auditor_id='{body.auditor_id}'")

    folder = COMPANIES_ROOT / body.company_name
    if not folder.exists():
        print(f"[mail] auditor: company folder not found -> {folder}")
        raise HTTPException(status_code=404, detail="Company folder not found")

    latest = _latest_docs(body.company_name)
    consent = latest.get("consent")
    if not consent:
        print("[mail] auditor: consent doc not found")
        raise HTTPException(status_code=400, detail="Auditor consent not generated")

    # Get full contact (name, email, membership_no)
    contact = _auditor_contact_by_id(body.auditor_id)
    if not contact:
        print(f"[mail] auditor: contact not found in master for auditor_id={body.auditor_id}")
        raise HTTPException(status_code=400, detail="Auditor contact not found in master")

    to_email = contact.get("email") or ""
    to_name  = (contact.get("name") or "").strip()
    mem_no   = (contact.get("membership_no") or "-").strip()

    if not to_email:
        print("[mail] auditor: contact has no email")
        raise HTTPException(status_code=400, detail="Auditor email not found in master")

    # Build company context from latest master
    _, master = _safe_master(body.company_name)
    company_ctx = _company_ctx_from_master(master, body.company_name)

    subject = f"Consent to Act as Auditor — {company_ctx['name']}"
    ctx = {
        "company": company_ctx,
        "auditor": {
            "name": to_name or "Auditor",
            "membership_no": mem_no
        },
        "contact": {
            "email": "contact@virtualfinanceservice.com",
            "phone": "+91 7738895510",
            "address": "Office 206, Shah Heritage CHS, Plot No.09, Sector 42, Nerul, Navi Mumbai 400706"
        },
        "reply_link": "mailto:contact@virtualfinanceservice.com",
    }

    print(f"[mail] auditor: rendering template for '{body.company_name}'")
    html = _render_html_template("emails/email_auditor_consent.html", ctx)
    print(f"[mail] auditor: template rendered ({len(html)} chars). Attachments: 1 -> {consent.name}")

    # Use friendly "Name <email>" if name is available
    to_header = f"{to_name} <{to_email}>" if to_name else to_email
    send_info = _send_email_smtp_ssl([to_header], subject, html, [consent])
    print(f"[mail] auditor: send result -> {send_info}")

    result = {
        "status": "OK" if send_info.get("sent") else "ERROR",
        "sent": bool(send_info.get("sent")),
        "reason": send_info.get("reason"),
        "to": {"auditor_id": body.auditor_id, "name": to_name, "email": to_email, "membership_no": mem_no},
        "subject": subject,
        "attachments": [{
            "filename": consent.name,
            "path": str(consent),
            "download_url": _dl_url(consent)
        }],
        "company": company_ctx["name"],
    }
    print("[mail] auditor:", result)
    return result


@router.post("/send/company")
def send_to_company(body: SendCompanyBody):
    folder = COMPANIES_ROOT / body.company_name
    if not folder.exists():
        raise HTTPException(status_code=404, detail="Company folder not found")

    latest = _latest_docs(body.company_name)
    missing = [k for k in ("bod", "intimation", "proposal") if k not in latest]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing docs: {', '.join(missing)}")

    # Director emails from latest _master_*.json (fallback to company email if available)
    mp, master = _safe_master(body.company_name)
    to_emails = _directors_emails_from_master(master)
    if not to_emails:
        raise HTTPException(status_code=400, detail="No director/company emails found in master")

    company_ctx = _company_ctx_from_master(master, body.company_name)
    subject = f"Documents for Signature — {company_ctx['name']}"

    # Context for directors email template
    att_list = [latest[k] for k in ("bod", "intimation", "proposal") if k in latest]
    ctx = {
        "company": company_ctx,
        "directors": master.get("directors") or [],
        "attachments": [{"name": p.name, "url": _dl_url(p)} for p in att_list],
        "contact": {
            "email": "contact@virtualfinanceservice.com",
            "phone": "+91 7738895510",
            "address": "Office 206, Shah Heritage CHS, Plot No.09, Sector 42, Nerul, Navi Mumbai 400706"
        },
        "reply_link": "mailto:contact@virtualfinanceservice.com",
    }

    # Render YOUR HTML file
    html = _render_html_template("emails/email_directors_pack.html", ctx)

    send_info = _send_email_smtp_ssl(to_emails, subject, html, att_list)

    result = {
        "status": "OK" if send_info.get("sent") else "ERROR",
        "sent": bool(send_info.get("sent")),
        "reason": send_info.get("reason"),
        "to": {"directors": to_emails},
        "subject": subject,
        "attachments": [{"filename": p.name, "path": str(p), "download_url": _dl_url(p)} for p in att_list],
        "company": company_ctx["name"],
    }
    print("[mail] company:", result)
    return result
