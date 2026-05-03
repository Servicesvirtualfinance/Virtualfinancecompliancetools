# app/routers/mail_sla.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
import json, re, smtplib, mimetypes
from typing import Optional, List

from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

from ..settings import (
    COMPANIES_ROOT, FRONTEND_ROOT,
    SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, SENDER_PASSWORD
)

router = APIRouter(prefix="/api/mail/sla", tags=["mail-sla"])

# ---------------- utils ----------------
def _log(msg: str): print(f"[mail_sla] {msg}")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
            if cur is None: break
        return "" if cur is None else str(cur)
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", lambda m: getter(m.group(1), ctx), tpl)

def _load_email_template() -> str:
    preferred = FRONTEND_ROOT / "emails" / "email_sla.html"
    fallback  = FRONTEND_ROOT / "emails" / "email_auditor_consent.html"
    if preferred.exists():
        _log(f"Using SLA email template: {preferred}")
        return preferred.read_text(encoding="utf-8")
    if fallback.exists():
        _log(f"Using FALLBACK SLA template: {fallback}")
        return fallback.read_text(encoding="utf-8")
    raise HTTPException(status_code=404, detail="No SLA email template found (email_sla.html or email_auditor_consent.html)")

def _latest_sla_doc(company_name: str) -> Optional[Path]:
    cdir = COMPANIES_ROOT / company_name
    if not cdir.exists():
        return None
    docs = sorted(cdir.glob("sla_agreement_TEMPLATE*.docx"))
    return docs[-1] if docs else None

def _first_valid_email(*candidates: Optional[str]) -> Optional[str]:
    for c in candidates:
        if not c: continue
        c = c.strip()
        if c and c != "-" and EMAIL_RE.match(c):
            return c
    return None

def _choose_company_recipient(master: dict) -> Optional[str]:
    # 1) company-level fields
    cand1 = master.get("email")
    cand2 = (master.get("contact") or {}).get("email") if isinstance(master.get("contact"), dict) else None
    # 2) director emails (if present)
    cand3 = None
    dirs = master.get("directors") or []
    for d in dirs:
        em = d.get("email") if isinstance(d, dict) else None
        if _first_valid_email(em):
            cand3 = em
            break
    # aggregate and choose the first valid
    chosen = _first_valid_email(cand1, cand2, cand3)
    _log(f"recipient candidates = company.email={cand1!r}, contact.email={cand2!r}, director.email={cand3!r} -> chosen={chosen!r}")
    return chosen

def _attach_files(msg: MIMEMultipart, attachments: List[Path]):
    for p in (attachments or []):
        try:
            ctype, enc = mimetypes.guess_type(str(p))
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
class SLABody(BaseModel):
    company_name: str
    to_email: Optional[str] = None   # optional override from UI

# ---------------- endpoints ----------------
@router.post("/preview", summary="Preview SLA email HTML")
def preview_sla(body: SLABody):
    _log(f"preview for: {body.company_name}")
    _, master = _safe_master(body.company_name)
    ctx = {
        "company": _company_ctx(master, body.company_name),
        "contact": {
            "email": "contact@virtualfinanceservice.com",
            "phone": "+91 7738895510",
            "address": "Office 206, Shah Heritage CHS, Plot No.09, Sector 42, Nerul, Navi Mumbai 400706",
            "name": "Virtual Finance"
        },
        "reply_link": "mailto:contact@virtualfinanceservice.com",
    }
    raw = _load_email_template()
    html = raw.replace('src="cid:vf_banner"', '/assets/img/Picture1.png')
    html = _render_vars(html, ctx)
    return {"html": html}

@router.post("/send", summary="Send SLA email with latest SLA .docx attached")
def send_sla(body: SLABody):
    _log(f"send for: {body.company_name}")
    _, master = _safe_master(body.company_name)
    company = _company_ctx(master, body.company_name)

    # choose recipient
    to_email = body.to_email.strip() if body.to_email else _choose_company_recipient(master)
    if not to_email:
        raise HTTPException(status_code=400, detail="No valid company email found. Please update master or pass 'to_email'.")

    if not EMAIL_RE.match(to_email):
        raise HTTPException(status_code=400, detail=f"Invalid recipient email: {to_email}")

    # SLA document
    sla_doc = _latest_sla_doc(body.company_name)
    if not sla_doc or not sla_doc.exists():
        raise HTTPException(status_code=400, detail="SLA document not found. Generate it first.")

    subject = f"SLA Agreement — {company['name']}"
    raw = _load_email_template()
    html = raw.replace('src="cid:vf_banner"', '/assets/img/Picture1.png')
    html = _render_vars(html, {
        "company": company,
        "contact": {
            "email": "contact@virtualfinanceservice.com",
            "phone": "+91 7738895510",
            "address": "Office 206, Shah Heritage CHS, Plot No.09, Sector 42, Nerul, Navi Mumbai 400706",
            "name": "Virtual Finance"
        },
        "reply_link": "mailto:contact@virtualfinanceservice.com",
    })

    send_info = _send_email_smtp_ssl([to_email], subject, html, [sla_doc])

    result = {
        "status": "OK" if send_info.get("sent") else "ERROR",
        "sent": bool(send_info.get("sent")),
        "reason": send_info.get("reason"),
        "to": {"email": to_email},
        "subject": subject,
        "attachments": [{
            "filename": sla_doc.name,
            "path": str(sla_doc),
            "download_url": f"/companies/{body.company_name}/{sla_doc.name}"
        }],
        "company": company["name"],
    }
    _log(f"send result: {result}")
    return result
