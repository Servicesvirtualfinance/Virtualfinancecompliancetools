# app/routers/mail_gst_signatory.py
# -*- coding: utf-8 -*-
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
import json, re, mimetypes, smtplib
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email import encoders

from ..settings import (
    COMPANIES_ROOT, FRONTEND_ROOT,
    SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, SENDER_PASSWORD
)

router = APIRouter(prefix="/api/mail/gst-signatory", tags=["mail-gst-signatory"])


BANNER_SIGNATORY = "https://raw.githubusercontent.com/virtualfinance1992/vf-assets/main/Authorised_Signatory.png"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------- utils ----------
def _log(m): print(f"[mail_gst_signatory] {m}")

def _find_company_dir(company_name: str) -> Path:
    exact = COMPANIES_ROOT / company_name
    if exact.exists(): return exact
    want = (company_name or "").casefold()
    for d in COMPANIES_ROOT.iterdir():
        if d.is_dir() and d.name.casefold() == want:
            return d
    raise HTTPException(404, "Company folder not found")

def _master_path(company_name: str) -> Path:
    cdir = _find_company_dir(company_name)
    masters = sorted(cdir.glob("_master_*.json"))
    if not masters:
        raise HTTPException(404, "Master not found for company")
    return masters[-1]

def _read_master(company_name: str) -> Dict[str, Any]:
    mp = _master_path(company_name)
    return json.loads(mp.read_text(encoding="utf-8"))

def _write_master(company_name: str, data: Dict[str, Any]) -> None:
    mp = _master_path(company_name)
    mp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _company_ctx(master: dict, company_name: str) -> dict:
    base = master.get("company") or {}
    addr = master.get("registered_address") or master.get("address") \
           or base.get("registered_address") or base.get("address")
    if isinstance(addr, dict):
        parts = [addr.get("line1"), addr.get("line2"), addr.get("city"), addr.get("state"), addr.get("pincode")]
        addr = ", ".join([str(x) for x in parts if x])
    return {
        "name": company_name,
        "cin": master.get("cin") or base.get("cin") or "-",
        "address": addr or "-",
        "email": master.get("email") or base.get("email") or "-"
    }

def _first_valid_email(*cands: Optional[str]) -> Optional[str]:
    for c in cands:
        if not c: continue
        c = c.strip()
        if EMAIL_RE.match(c): return c
    return None

def _choose_company_recipient(master: dict) -> Optional[str]:
    cand1 = master.get("email")
    cand2 = (master.get("contact") or {}).get("email") if isinstance(master.get("contact"), dict) else None
    cand3 = None
    for d in (master.get("directors") or []):
        if _first_valid_email(d.get("email")):
            cand3 = d.get("email"); break
    chosen = _first_valid_email(cand1, cand2, cand3)
    _log(f"recipient candidates: company={cand1!r} contact={cand2!r} director={cand3!r} → {chosen!r}")
    return chosen

def _latest_signatory_doc(company_name: str, master: dict) -> Path:
    # Prefer path saved by the generation step
    rec = master.get("authorized_signatory") or master.get("authorised_signatory") or {}
    if isinstance(rec, dict):
        p = rec.get("docx")
        if p and Path(p).exists():
            return Path(p)

    # Fallback: newest bod_gst_authorized_signatory_*.docx in company folder
    cdir = _find_company_dir(company_name)
    docs = sorted(cdir.glob("bod_gst_authorized_signatory_*.docx"))
    if docs:
        return docs[-1]
    raise HTTPException(404, "No generated resolution found for this company")

def _attach_files(msg: MIMEMultipart, files: List[Path]):
    for p in files or []:
        ctype, _ = mimetypes.guess_type(str(p))
        if not ctype: ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(p, "rb") as f:
            part = MIMEBase(maintype, subtype)
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=p.name)
        msg.attach(part)
        _log(f"attached: {p}")

def _send_email_smtp_ssl(to_addrs: List[str], subject: str, html: str, attachments: Optional[List[Path]] = None):
    msg = MIMEMultipart("mixed")
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    _attach_files(msg, attachments or [])

    try:
        _log(f"SMTP connect {SMTP_SERVER}:{SMTP_PORT} as {SENDER_EMAIL}")
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as s:
            s.login(SENDER_EMAIL, SENDER_PASSWORD)
            s.sendmail(SENDER_EMAIL, to_addrs, msg.as_string())
        _log("SMTP sent OK")
        return {"sent": True}
    except Exception as e:
        _log(f"SMTP error: {e!r}")
        return {"sent": False, "reason": str(e)}

def _load_email_template() -> str:
    path = FRONTEND_ROOT / "emails" / "email_gst_signatory.html"
    if not path.exists():
        raise HTTPException(404, "Email template not found: emails/email_gst_signatory.html")
    return path.read_text(encoding="utf-8")

def _render_vars(tpl: str, ctx: dict) -> str:
    # very small {{ a.b }} renderer like you used before
    def getter(path, data):
        cur = data
        for k in [p.strip() for p in path.split(".")]:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None: break
        return "" if cur is None else str(cur)
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", lambda m: getter(m.group(1), ctx), tpl)

# ---------- models ----------
class Body(BaseModel):
    company_name: str
    to_email: Optional[str] = None
    contact_name: Optional[str] = None  # for greeting

# ---------- endpoints ----------
@router.post("/preview", summary="Preview email for sending GST Authorised Signatory Resolution")
def preview_email(body: Body):
    master = _read_master(body.company_name)
    company = _company_ctx(master, body.company_name)
    docx = _latest_signatory_doc(body.company_name, master)

    sign = (master.get("authorized_signatory") or master.get("authorised_signatory") or {})
    ctx = {
        "company": company,
        "signatory": {
            "name": sign.get("name") or "",
            "din":  sign.get("din") or "-",
            "designation": sign.get("designation") or "Director",
            "meeting_date": sign.get("meeting_date") or datetime.now().strftime("%d %b %Y"),
            "meeting_place": sign.get("meeting_place") or "Registered Office",
        },
        "contact": {
            "name": body.contact_name or "Sir/Madam",
            "email": company["email"],
        },
        "today": datetime.now().strftime("%d %b %Y"),
        "banner_src": BANNER_SIGNATORY,
        "attachment_name": docx.name,
    }
    html = _render_vars(_load_email_template(), ctx)
    return {"html": html, "attachment": docx.name}

@router.post("/send", summary="Send GST Authorised Signatory Resolution email with attachment")
def send_email(body: Body):
    master = _read_master(body.company_name)
    company = _company_ctx(master, body.company_name)
    docx = _latest_signatory_doc(body.company_name, master)

    # choose recipient
    to_email = (body.to_email or "").strip() or _choose_company_recipient(master)
    if not to_email:
        raise HTTPException(400, "No valid recipient email found (pass to_email or update master).")
    if not EMAIL_RE.match(to_email):
        raise HTTPException(400, f"Invalid recipient email: {to_email}")

    sign = (master.get("authorized_signatory") or master.get("authorised_signatory") or {})
    ctx = {
        "company": company,
        "signatory": {
            "name": sign.get("name") or "",
            "din":  sign.get("din") or "-",
            "designation": sign.get("designation") or "Director",
            "meeting_date": sign.get("meeting_date") or datetime.now().strftime("%d %b %Y"),
            "meeting_place": sign.get("meeting_place") or "Registered Office",
        },
        "contact": { "name": body.contact_name or "Sir/Madam", "email": company["email"] },
        "today": datetime.now().strftime("%d %b %Y"),
        "banner_src": BANNER_SIGNATORY,
    }
    subject = f"GST Authorised Signatory Resolution — {company['name']}"
    html = _render_vars(_load_email_template(), ctx)

    result = _send_email_smtp_ssl([to_email], subject, html, attachments=[docx])

    # log outcome into master
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    plog = master.get("process_log") or []
    plog.append({
        "process": "MAIL_GST_AUTH_SIGNATORY",
        "ts": ts,
        "inputs": {"to": to_email},
        "outputs": {"attachment": docx.name},
        "status": "SUCCESS" if result.get("sent") else "ERROR",
        "reason": result.get("reason"),
    })
    master["process_log"] = plog
    rec = master.get("authorized_signatory") or master.get("authorised_signatory") or {}
    rec["mailed"] = bool(result.get("sent"))
    rec["mailed_at"] = ts
    master["authorized_signatory"] = rec
    master["authorised_signatory"] = rec
    _write_master(body.company_name, master)

    return {
        "status": "OK" if result.get("sent") else "ERROR",
        "to": {"email": to_email},
        "subject": subject,
        "attachment": docx.name,
        "reason": result.get("reason"),
    }
