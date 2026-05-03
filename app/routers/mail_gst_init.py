# app/routers/mail_gst_init.py
# -*- coding: utf-8 -*-
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
from typing import Optional, List
import json, re, smtplib, mimetypes

from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

from ..settings import (
    COMPANIES_ROOT, FRONTEND_ROOT,
    SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, SENDER_PASSWORD
)

router = APIRouter(prefix="/api/mail/gst-init", tags=["mail-gst-init"])
BANNER_GST = "https://raw.githubusercontent.com/virtualfinance1992/vf-assets/main/GST%20Initiation.png"

# ---------------- utils ----------------
def _log(msg: str):
    print(f"[mail_gst_init] {msg}")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _is_null_company(company_name: Optional[str]) -> bool:
    return (not company_name) or (company_name.strip().upper() == "NULL")

def _safe_master(company_name: str):
    cdir = COMPANIES_ROOT / company_name
    if not cdir.exists():
        raise HTTPException(status_code=404, detail="Company folder not found")
    masters = sorted(cdir.glob("_master_*.json"))
    if not masters:
        raise HTTPException(status_code=404, detail="Master not found for company")
    mp = masters[-1]
    data = json.loads(mp.read_text(encoding="utf-8"))
    return mp, data

def _company_ctx(master: dict, company_name: str):
    return {
        "name": company_name,
        "cin": master.get("cin") or "-",
        "pan": (master.get("pan") or {}).get("pan_number") if isinstance(master.get("pan"), dict) else (master.get("pan") or "-"),
        "tan": master.get("tan") or "-",
        "email": master.get("email") or "-"
    }

def _render_vars(tpl: str, ctx: dict) -> str:
    def getter(path, data):
        cur = data
        for k in [p.strip() for p in path.split(".")]:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        return "" if cur is None else str(cur)
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", lambda m: getter(m.group(1), ctx), tpl)

def _load_email_template() -> str:
    tpl = FRONTEND_ROOT / "emails" / "email_gst_init.html"
    if tpl.exists():
        _log(f"Using GST email template: {tpl}")
        return tpl.read_text(encoding="utf-8")
    raise HTTPException(status_code=404, detail="GST email template not found (emails/email_gst_init.html)")

def _first_valid_email(*candidates: Optional[str]) -> Optional[str]:
    for c in candidates:
        if not c:
            continue
        c = c.strip()
        if c and c != "-" and EMAIL_RE.match(c):
            return c
    return None

def _choose_company_recipient(master: dict) -> Optional[str]:
    # 1) company-level fields
    cand1 = master.get("email")
    cand2 = (master.get("contact") or {}).get("email") if isinstance(master.get("contact"), dict) else None
    # 2) director emails (fallback)
    cand3 = None
    dirs = master.get("directors") or []
    for d in dirs:
        em = d.get("email") if isinstance(d, dict) else None
        if _first_valid_email(em):
            cand3 = em
            break
    chosen = _first_valid_email(cand1, cand2, cand3)
    _log(f"recipient candidates = company.email={cand1!r}, contact.email={cand2!r}, director.email={cand3!r} -> chosen={chosen!r}")
    return chosen

def _attach_files(msg: MIMEMultipart, attachments: List[Path]):
    for p in (attachments or []):
        try:
            ctype, _ = mimetypes.guess_type(str(p))
            if ctype is None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(p, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=p.name)
            msg.attach(part)
            _log(f"attached: {p}")
        except Exception as e:
            _log(f"attach error for {p}: {e!r}")

def _send_email_smtp_ssl(to_addrs: List[str], subject: str, html: str, attachments: Optional[List[Path]] = None):
    attachments = attachments or []
    msg = MIMEMultipart("mixed")
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    _attach_files(msg, attachments)

    try:
        _log(f"SMTP connect {SMTP_SERVER}:{SMTP_PORT} as {SENDER_EMAIL}")
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, to_addrs, msg.as_string())
        _log("SMTP sent OK")
        return {"sent": True}
    except Exception as e:
        _log(f"SMTP SEND ERROR: {e!r}")
        return {"sent": False, "reason": str(e)}

# ---------------- models ----------------
class GSTInitBody(BaseModel):
    company_name: str                   # "NULL" indicates new customer (no master lookup)
    to_email: Optional[str] = None      # required when company_name == "NULL"
    contact_name: Optional[str] = None  # optional greeting

# ---------------- endpoints ----------------
@router.post("/preview", summary="Preview GST documents request email")
def preview_gst(body: GSTInitBody):
    _log(f"preview for: {body.company_name!r}")
    # For preview: if company is NULL, don't touch master; show generic
    if _is_null_company(body.company_name):
        company = {"name": "New Customer"}
    else:
        _, master = _safe_master(body.company_name)
        company = _company_ctx(master, body.company_name)

    ctx = {
        "company": company,
        "contact": {
            "name": (body.contact_name or "Sir/Madam"),
            "email": "contact@virtualfinanceservice.com",
            "phone": "+91 7738895510",
        },
        "today": datetime.now().strftime("%d %b %Y"),
        "reply_link": "mailto:contact@virtualfinanceservice.com",
        "banner_src": BANNER_GST,
    }
    raw = _load_email_template()
    html = _render_vars(raw, ctx)
    return {"html": html}

@router.post("/send", summary="Send GST documents request email")
def send_gst(body: GSTInitBody):
    _log(f"send for: {body.company_name!r}")

    # NEW CUSTOMER MODE: company_name is "NULL" (or blank) -> require to_email; skip master
    if _is_null_company(body.company_name):
        to_email = (body.to_email or "").strip()
        if not to_email:
            raise HTTPException(status_code=400, detail="to_email required for new customer")
        if not EMAIL_RE.match(to_email):
            raise HTTPException(status_code=400, detail=f"Invalid recipient email: {to_email}")
        company = {"name": "New Customer"}
        subject = "GST Registration — Documents Checklist & Next Steps"
    else:
        # MASTER MODE
        _, master = _safe_master(body.company_name)
        company = _company_ctx(master, body.company_name)

        # respect explicit to_email, otherwise pick from master
        to_email = (body.to_email or "").strip() or _choose_company_recipient(master)
        if not to_email:
            raise HTTPException(status_code=400, detail="No valid recipient email found. Update master or pass 'to_email'.")
        if not EMAIL_RE.match(to_email):
            raise HTTPException(status_code=400, detail=f"Invalid recipient email: {to_email}")

        subject = f"GST Registration — Documents Checklist & Next Steps | {company['name']}"

    ctx = {
        "company": company,
        "contact": {
            "name": (body.contact_name or "Sir/Madam"),
            "email": "contact@virtualfinanceservice.com",
            "phone": "+91 7738895510",
        },
        "today": datetime.now().strftime("%d %b %Y"),
        "reply_link": "mailto:contact@virtualfinanceservice.com",
        "banner_src": BANNER_GST,   # use the GST banner for send as well
    }
    raw = _load_email_template()
    html = _render_vars(raw, ctx)

    send_info = _send_email_smtp_ssl([to_email], subject, html, attachments=[])

    result = {
        "status": "OK" if send_info.get("sent") else "ERROR",
        "sent": bool(send_info.get("sent")),
        "reason": send_info.get("reason"),
        "to": {"email": to_email},
        "subject": subject,
        "company": company.get("name"),
    }
    _log(f"send result: {result}")
    return result
