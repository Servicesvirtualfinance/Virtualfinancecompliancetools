# app/routers/mail_udyam.py
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

router = APIRouter(prefix="/api/mail/udyam", tags=["mail-udyam"])

BANNER_UDYAM = "https://raw.githubusercontent.com/virtualfinance1992/vf-assets/main/MSME_Registeration.png"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --------- utils ----------
def _log(m): print(f"[mail_udyam] {m}")

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

def _find_udyam_pdf(company_name: str, master: dict) -> Path:
    """
    Locate the Udyam certificate PDF.
    Priority:
      1) master['udyam']['pdf'] if present
      2) newest file in company dir (recursive) matching /ud[hy]?yam.*\.pdf/i
    """
    cdir = _find_company_dir(company_name)

    # 1) master hint
    u = master.get("udyam") or {}
    if isinstance(u, dict):
        p = u.get("pdf") or u.get("path")
        if p:
            pp = Path(p)
            if not pp.is_absolute():
                pp = cdir / p
            if pp.exists():
                return pp

    # 2) scan
    rx = re.compile(r"ud[hy]?yam.*\.pdf$", re.IGNORECASE)
    candidates: List[Path] = []
    for p in cdir.rglob("*.pdf"):
        if rx.search(p.name):
            candidates.append(p)
    if not candidates:
        raise HTTPException(
            404,
            "Udyam certificate not found. Expected a file like 'UDYAM_*.pdf' or 'UDHYAM_*.pdf' inside the company folder."
        )
    # newest by mtime
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1]

def _attach_files(msg: MIMEMultipart, files: List[Path]):
    for p in files or []:
        ctype, _ = mimetypes.guess_type(str(p))
        if not ctype: ctype = "application/pdf"
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
    path = FRONTEND_ROOT / "emails" / "email_udyam.html"
    if not path.exists():
        raise HTTPException(404, "Email template not found: emails/email_udyam.html")
    return path.read_text(encoding="utf-8")

def _render_vars(tpl: str, ctx: dict) -> str:
    # tiny {{ a.b }} renderer
    def getter(path, data):
        cur = data
        for k in [p.strip() for p in path.split(".")]:
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None: break
        return "" if cur is None else str(cur)
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", lambda m: getter(m.group(1), ctx), tpl)

# --------- models ----------
class Body(BaseModel):
    company_name: str
    to_email: Optional[str] = None
    contact_name: Optional[str] = None  # greeting

# --------- endpoints ----------
@router.post("/preview", summary="Preview email for Udyam certificate")
def preview_email(body: Body):
    master = _read_master(body.company_name)
    company = _company_ctx(master, body.company_name)
    pdf = _find_udyam_pdf(body.company_name, master)

    # try to show registration no. if present in master
    u = master.get("udyam") or {}
    reg = u.get("registration_no") or u.get("udyam_number") or ""

    ctx = {
        "company": company,
        "udyam": {
            "registration_no": reg,
            "file_name": pdf.name,
        },
        "contact": { "name": body.contact_name or "Sir/Madam", "email": company["email"] },
        "today": datetime.now().strftime("%d %b %Y"),
        "banner_src": BANNER_UDYAM,
    }
    html = _render_vars(_load_email_template(), ctx)
    return {"html": html, "attachment": pdf.name}

@router.post("/send", summary="Send Udyam certificate email with attachment")
def send_email(body: Body):
    master = _read_master(body.company_name)
    company = _company_ctx(master, body.company_name)
    pdf = _find_udyam_pdf(body.company_name, master)

    # recipient
    to_email = (body.to_email or "").strip() or _choose_company_recipient(master)
    if not to_email:
        raise HTTPException(400, "No valid recipient email found (pass to_email or update master).")
    if not EMAIL_RE.match(to_email):
        raise HTTPException(400, f"Invalid recipient email: {to_email}")

    u = master.get("udyam") or {}
    reg = u.get("registration_no") or u.get("udyam_number") or ""

    ctx = {
        "company": company,
        "udyam": {
            "registration_no": reg,
            "file_name": pdf.name,
        },
        "contact": { "name": body.contact_name or "Sir/Madam", "email": company["email"] },
        "today": datetime.now().strftime("%d %b %Y"),
        "banner_src": BANNER_UDYAM,
    }
    subject = f"Udyam Registration Certificate — {company['name']}"
    html = _render_vars(_load_email_template(), ctx)

    res = _send_email_smtp_ssl([to_email], subject, html, attachments=[pdf])

    # log outcome in master + stamp path
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    plog = master.get("process_log") or []
    plog.append({
        "process": "MAIL_UDYAM_CERT",
        "ts": ts,
        "inputs": {"to": to_email},
        "outputs": {"attachment": pdf.name},
        "status": "SUCCESS" if res.get("sent") else "ERROR",
        "reason": res.get("reason"),
    })
    master["process_log"] = plog

    udyam_rec = master.get("udyam") or {}
    # store relative path for portability
    try:
        rel = str(pdf.relative_to(_find_company_dir(body.company_name)))
    except Exception:
        rel = str(pdf)
    udyam_rec["pdf"] = rel
    udyam_rec["mailed"] = bool(res.get("sent"))
    udyam_rec["mailed_at"] = ts
    master["udyam"] = udyam_rec
    _write_master(body.company_name, master)

    return {
        "status": "OK" if res.get("sent") else "ERROR",
        "to": {"email": to_email},
        "subject": subject,
        "attachment": pdf.name,
        "reason": res.get("reason"),
    }
