# app/routers/mail_adt1_ack.py
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from pathlib import Path
from typing import Optional, List
import json, re, mimetypes, ssl, smtplib
from email.message import EmailMessage

from app.settings import (
    COMPANIES_ROOT,
    SENDER_EMAIL,
    SENDER_PASSWORD,
    SMTP_SERVER,
    SMTP_PORT,
)

router = APIRouter(prefix="/api/mail/adt1-ack", tags=["mail-adt1-ack"])

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
SRN_RE   = re.compile(r"\b[AW]\d{6,}\b", re.I)  # e.g., W123456 / A123456 style

# === TEMPLATE & IMAGE ===
TEMPLATE_ACK_FILE = Path(r"E:\Bots\docgen_backend_minimal_v2\frontend\emails\email_adt1_ack.html")
# Prefer your own domain asset; raw GitHub URL works as a fallback.
IMAGE_URL = (
    "https://raw.githubusercontent.com/virtualfinance1992/vf-assets/"
    "e22ad166c0128779105d0995be521514bd9c8073/"
    "Attach%20DSC%20to%20Form%20No.01%20ADT-1.png"
)

# ---------- helpers ----------
def _company_dir_by_name(name: str) -> Optional[Path]:
    if not name:
        return None
    target = name.strip().lower()
    root = Path(COMPANIES_ROOT)
    # exact
    for p in root.glob("*"):
        if p.is_dir() and p.name.strip().lower() == target:
            return p
    # soft
    for p in root.glob("*"):
        if p.is_dir() and target in p.name.strip().lower():
            return p
    return None

def _latest_master_json(cdir: Path) -> Optional[Path]:
    files = sorted(cdir.glob("_master_*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    return files[0] if files else None

def _deep_find_email(obj) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        m = EMAIL_RE.search(obj)
        return m.group(0) if m else None
    if isinstance(obj, dict):
        for k in ("email", "company_email", "Email Id", "Email", "official_email"):
            if k in obj and isinstance(obj[k], str):
                m = EMAIL_RE.search(obj[k])
                if m: return m.group(0)
        for v in obj.values():
            e = _deep_find_email(v)
            if e: return e
    if isinstance(obj, list):
        for v in obj:
            e = _deep_find_email(v)
            if e: return e
    return None

def _split_emails(csv: Optional[str]) -> List[str]:
    if not csv:
        return []
    out = []
    for part in (x.strip() for x in csv.split(",") if x.strip()):
        if re.search(r"@.+\.", part):
            out.append(part)
    return out

def _attach_file(msg: EmailMessage, file_path: Path):
    ctype, _ = mimetypes.guess_type(str(file_path))
    if not ctype:
        ctype = "application/pdf"
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(file_path.read_bytes(), maintype=maintype, subtype=subtype, filename=file_path.name)

def _extract_srn_from_text(txt: str) -> Optional[str]:
    m = SRN_RE.search(txt or "")
    return m.group(0).upper() if m else None

def _extract_srn_from_master(cdir: Path) -> Optional[str]:
    mpath = _latest_master_json(cdir)
    if not (mpath and mpath.exists()):
        return None
    try:
        data = json.loads(mpath.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None

    # Scan obvious keys first
    def scan(obj) -> Optional[str]:
        if obj is None: return None
        if isinstance(obj, str): return _extract_srn_from_text(obj)
        if isinstance(obj, dict):
            for k in ("srn", "SRN", "mca_srn", "ack_srn", "adt1_srn", "acknowledgement_srn"):
                if k in obj and isinstance(obj[k], str):
                    s = _extract_srn_from_text(obj[k])
                    if s: return s
            for v in obj.values():
                s = scan(v)
                if s: return s
        if isinstance(obj, list):
            for v in obj:
                s = scan(v)
                if s: return s
        return None

    return scan(data)

def _auto_pick_ack(cdir: Path) -> Optional[str]:
    """
    Prefer a company-specific file named exactly 'ack_ADT1.pdf'
    (case-insensitive, first in root, then anywhere under the company folder).
    If not found, fall back to the scoring heuristic used earlier.
    """
    # 1) Exact filename in the company root (fast path)
    direct = cdir / "ack_ADT1.pdf"
    if direct.exists():
        return str(direct)

    # 2) Case-insensitive match in the company root
    for p in cdir.glob("*.pdf"):
        if p.name.lower() == "ack_adt1.pdf":
            return str(p)

    # 3) Case-insensitive match anywhere under the company folder
    for p in cdir.rglob("*.pdf"):
        if p.name.lower() == "ack_adt1.pdf":
            return str(p)

    # 4) Fallback: previous ranking logic (kept verbatim)
    candidates: list[tuple[int, float, str]] = []
    for p in cdir.rglob("*.pdf"):
        n = p.name.lower()

        # must be related to ADT-1 or acknowledgement
        if not (("adt-1" in n or "adt1" in n or " adt " in n or " adt-" in n or "adt_1" in n) or
                ("ack" in n or "acknowledgement" in n or "receipt" in n or "challan" in n or "srn" in n)):
            continue

        score = 0
        if "acknowledgement" in n: score += 120
        if re.search(r"\back\b", n): score += 110
        if "receipt" in n: score += 90
        if "challan" in n: score += 80
        if re.search(r"\bsrn\b", n): score += 70
        if re.search(r"\bform\b", n) and re.search(r"adt[-\s]?1", n): score += 50
        if ("adt-1" in n or "adt1" in n): score += 20
        if "board" in n: score -= 80
        if "resolution" in n or re.search(r"\bbr\b", n): score -= 70
        if "draft" in n: score -= 30
        if "unsigned" in n or "working" in n: score -= 20

        candidates.append((score, p.stat().st_mtime, str(p)))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]


def _load_template_html(company: str, note: Optional[str], srn: Optional[str], logo_src: Optional[str]) -> str:
    safe_company = (company or "").strip() or "Your Company"
    safe_note = (note or "").strip()
    safe_srn = (srn or "").strip()

    try:
        if TEMPLATE_ACK_FILE.exists():
            raw = TEMPLATE_ACK_FILE.read_text(encoding="utf-8", errors="ignore")
            html = (raw
                .replace("{{ company.name }}", safe_company)
                .replace("{{ note }}", safe_note)
                .replace("{{ srn }}", safe_srn)
                .replace("{{ logo_src }}", logo_src or "")
            )
            return html
    except Exception:
        pass

    # Minimal fallback if template missing
    title = f"ADT-1 Acknowledgement — {safe_company}" + (f" (SRN: {safe_srn})" if safe_srn else "")
    note_html = f"<p style='margin:0 0 10px 0;color:#334155'>{safe_note}</p>" if safe_note else ""
    return (
        "<!doctype html><html><body style=\"font-family:Segoe UI,Roboto,Arial,sans-serif;background:#f6f7fb;padding:0;margin:0\">"
        "<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:640px;margin:24px auto;background:#ffffff;border-radius:12px;overflow:hidden\">"
        f"<tr><td style=\"padding:0\"><img src=\"{logo_src or ''}\" alt=\"Virtual Finance\" width=\"640\" "
        "style=\"display:block;width:100%;height:auto;border:0;\"></td></tr>"
        f"<tr><td style=\"padding:20px 24px\"><h2 style=\"margin:0;color:#0f172a;font-size:20px\">{title}</h2>"
        f"{note_html}"
        "<p style=\"margin:12px 0 0 0;color:#334155\">Please find attached the MCA acknowledgement/receipt for ADT-1 filing.</p>"
        "<p style=\"margin:0;color:#64748b;font-size:12px\">Regards,<br/>Team Virtual Finance</p>"
        "</td></tr></table></body></html>"
    )

# ---------- models ----------
class AckPreviewIn(BaseModel):
    company_name: str
    to_email: Optional[str] = None
    note: Optional[str] = None
    cc: Optional[str] = None

# ---------- endpoints ----------
@router.post("/preview")
def ack_preview(payload: AckPreviewIn):
    company = (payload.company_name or "").strip()
    if not company:
        raise HTTPException(400, detail="company_name required")

    cdir = _company_dir_by_name(company)
    if not cdir:
        raise HTTPException(404, detail=f"Company folder not found: {company}")

    # Resolve recipient (prefer payload; else from master)
    to_email = (payload.to_email or "").strip()
    if not to_email:
        mpath = _latest_master_json(cdir)
        if mpath and mpath.exists():
            try:
                data = json.loads(mpath.read_text(encoding="utf-8", errors="ignore"))
                to_email = _deep_find_email(data) or ""
            except Exception:
                pass

    # Try to guess SRN from master or filenames
    srn = _extract_srn_from_master(cdir) or _extract_srn_from_text(" ".join(p.name for p in cdir.glob("*.pdf")))
    html = _load_template_html(cdir.name, payload.note, srn, IMAGE_URL)

    return {"ok": True, "to_email": (to_email or None), "srn": (srn or None), "html": html}

@router.post("/send")
async def ack_send(
    company_name: str = Form(...),
    to_email: str = Form(""),
    note: str = Form(""),
    cc: str = Form(""),
    ack_pdf: UploadFile | None = File(None),
):
    company = (company_name or "").strip()
    if not company:
        raise HTTPException(400, detail="company_name required")

    cdir = _company_dir_by_name(company)
    if not cdir:
        raise HTTPException(404, detail=f"Company folder not found: {company}")

    # Resolve recipient
    to = (to_email or "").strip()
    if not to:
        mpath = _latest_master_json(cdir)
        if mpath and mpath.exists():
            try:
                data = json.loads(mpath.read_text(encoding="utf-8", errors="ignore"))
                to = _deep_find_email(data) or ""
            except Exception:
                pass
    if not to:
        raise HTTPException(400, detail="Recipient email missing or not found in master.")

    # SRN for subject/body
    srn = _extract_srn_from_master(cdir)

    subject = f"ADT-1 Acknowledgement — {cdir.name}" + (f" (SRN: {srn})" if srn else "")
    html = _load_template_html(cdir.name, note.strip() or None, srn, IMAGE_URL)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = to
    cc_list = _split_emails(cc)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.set_content("Please view this message in HTML.")
    msg.add_alternative(html, subtype="html")

    # Attach the acknowledgement (uploaded overrides auto-pick)
    attachments = []
    if ack_pdf and ack_pdf.filename:
        tmp = Path(COMPANIES_ROOT).parent / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        save_as = tmp / ack_pdf.filename
        save_as.write_bytes(await ack_pdf.read())
        _attach_file(msg, save_as)
        attachments.append(str(save_as))
    else:
        auto = _auto_pick_ack(cdir)
        if not auto:
            raise HTTPException(400, detail="Acknowledgement/Receipt PDF not found in company folder and no file uploaded.")
        _attach_file(msg, Path(auto))
        attachments.append(auto)

    # Send SMTP
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ctx) as s:
            s.login(SENDER_EMAIL, SENDER_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        raise HTTPException(500, detail=f"SMTP send failed: {e}")

    return {"sent": True, "to": to, "cc": cc_list, "subject": subject, "attachments": attachments, "srn": (srn or None)}
