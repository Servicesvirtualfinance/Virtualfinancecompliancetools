# app/routers/mail_adt1.py
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

router = APIRouter(prefix="/api/mail/adt1", tags=["mail-adt1"])

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)

# === Path to the HTML email template (SLA-styled ADT-1) ===
# If you prefer, move this into app.settings and import from there.
TEMPLATE_ADT1_FILE = Path(r"E:\Bots\docgen_backend_minimal_v2\frontend\emails\email_adt1.html")


# ---------- helpers (self-contained; no new router) ----------
def _company_dir_by_name(name: str) -> Optional[Path]:
    """Resolve company folder by exact case-insensitive name, else contains()."""
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
        # prefer obvious keys first
        for k in ("email", "company_email", "Email Id", "Email", "official_email"):
            if k in obj and isinstance(obj[k], str):
                m = EMAIL_RE.search(obj[k])
                if m:
                    return m.group(0)
        for v in obj.values():
            e = _deep_find_email(v)
            if e:
                return e
    if isinstance(obj, list):
        for v in obj:
            e = _deep_find_email(v)
            if e:
                return e
    return None


def _auto_pick_adt1(cdir: Path) -> Optional[str]:
    """
    Rank all ADT-1 related PDFs and return the best candidate.

    Preference order:
      1) Filenames like "Form No. ADT-1.pdf" / "Form ADT-1 ..."
      2) Other files containing ADT-1 / ADT1
    Penalize: "board", "resolution", "br", "signed", "draft", "scan", "photo", "image"
    Tie-breaker: most recent mtime.
    """
    candidates: list[tuple[int, float, str]] = []

    for p in cdir.rglob("*.pdf"):
        n = p.name.lower()

        # must look like an ADT-1 doc at all
        if not ("adt-1" in n or "adt1" in n or " adt " in n or " adt-" in n or " adt1" in n or "adt 1" in n or "adt_" in n):
            continue

        score = 0

        # strong positives for "form ... adt-1"
        if re.search(r"\bform\s*no\.?\s*[- ]?\s*adt[-\s]?1\b", n):
            score += 120
        if re.search(r"\bform\b.*\badt[-\s]?1\b", n):
            score += 100

        # general ADT-1 signals
        if "adt-1" in n or "adt1" in n:
            score += 30

        # common negatives we don't want as primary attachment
        if "board" in n:
            score -= 60
        if "resolution" in n or re.search(r"\bbr\b", n):
            score -= 60
        if "signed" in n or "sign" in n:
            score -= 40
        if "draft" in n:
            score -= 30
        if "scan" in n or "scanned" in n or "photo" in n or "image" in n:
            score -= 20

        # prefer more recent if scores tie
        mtime = p.stat().st_mtime
        candidates.append((score, mtime, str(p)))

    if not candidates:
        return None

    # highest score first, then most recent
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]



def _load_template_html(company: str, note: Optional[str]) -> str:
    """
    Load the SLA-styled ADT-1 email HTML from disk and inject simple placeholders.
    Supported placeholders in the HTML file:
      - {{ company.name }}
      - {{ note }}
    If the file isn't found or fails to read, we fall back to a minimal inline HTML.
    """
    safe_company = (company or "").strip() or "Your Company"
    safe_note = (note or "").strip()

    try:
        if TEMPLATE_ADT1_FILE.exists():
            raw = TEMPLATE_ADT1_FILE.read_text(encoding="utf-8", errors="ignore")

            # Very small, safe templating: replace {{ company.name }} and {{ note }}
            html = (
                raw
                .replace("{{ company.name }}", safe_company)
                .replace("{{ note }}", safe_note)
            )
            return html
    except Exception:
        # fall through to fallback below
        pass

    # Fallback minimal HTML (kept consistent with earlier tone)
    note_html = f"<p style='margin:0 0 10px 0;color:#334155'>{safe_note}</p>" if safe_note else ""
    return (
        "<!doctype html><html><body style=\"font-family:Segoe UI,Roboto,Arial,sans-serif;background:#f6f7fb;padding:0;margin:0\">"
        "<table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:640px;margin:24px auto;background:#ffffff;border-radius:12px;overflow:hidden\">"
        "<tr><td style=\"padding:20px 24px\">"
        "<h2 style=\"margin:0 0 6px 0;color:#0f172a;font-size:20px\">Form ADT-1 – Request for Digital Signature</h2>"
        f"<p style=\"margin:0 0 12px 0;color:#334155\">Dear Team at <strong>{safe_company}</strong>,</p>"
        f"{note_html}"
        "<p style=\"margin:0;color:#334155\">We have attached <strong>Form No. ADT-1</strong>. "
        "Please attach/apply your DSC on the form and send it back to us to complete the ADT-1 filing process.</p>"
        "<hr style=\"border:none;border-top:1px solid #e5e7eb;margin:16px 0\"/>"
        "<p style=\"margin:0;color:#64748b;font-size:12px\">Regards,<br/>Team Virtual Finance</p>"
        "</td></tr></table></body></html>"
    )


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


# ---------- models ----------
class ADT1PreviewIn(BaseModel):
    company_name: str
    to_email: Optional[str] = None
    note: Optional[str] = None
    cc: Optional[str] = None


# ---------- endpoints ----------
@router.post("/preview")
def adt1_preview(payload: ADT1PreviewIn):
    company = (payload.company_name or "").strip()
    if not company:
        raise HTTPException(400, detail="company_name required")
    cdir = _company_dir_by_name(company)
    if not cdir:
        raise HTTPException(404, detail=f"Company folder not found: {company}")

    # resolve recipient (prefer payload; else from master)
    to_email = (payload.to_email or "").strip()
    if not to_email:
        mpath = _latest_master_json(cdir)
        if mpath and mpath.exists():
            try:
                data = json.loads(mpath.read_text(encoding="utf-8", errors="ignore"))
                to_email = _deep_find_email(data) or ""
            except Exception:
                pass

    html = _load_template_html(cdir.name, payload.note)
    return {"ok": True, "to_email": (to_email or None), "html": html}


@router.post("/send")
async def adt1_send(
    company_name: str = Form(...),
    to_email: str = Form(""),
    note: str = Form(""),
    cc: str = Form(""),
    adt1_pdf: UploadFile | None = File(None),
):
    company = (company_name or "").strip()
    if not company:
        raise HTTPException(400, detail="company_name required")
    cdir = _company_dir_by_name(company)
    if not cdir:
        raise HTTPException(404, detail=f"Company folder not found: {company}")

    # resolve recipient
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

    # build email
    subject = f"Form ADT-1 for Digital Signature — {cdir.name}"
    html = _load_template_html(cdir.name, note.strip() or None)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = to
    cc_list = _split_emails(cc)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.set_content("Please view this message in HTML.")
    msg.add_alternative(html, subtype="html")

    # attach PDF (uploaded overrides auto-pick)
    attachments = []
    if adt1_pdf and adt1_pdf.filename:
        tmp = Path(COMPANIES_ROOT).parent / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        save_as = tmp / adt1_pdf.filename
        save_as.write_bytes(await adt1_pdf.read())
        _attach_file(msg, save_as)
        attachments.append(str(save_as))
    else:
        auto = _auto_pick_adt1(cdir)
        if not auto:
            raise HTTPException(400, detail="ADT-1 PDF not found in company folder and no file uploaded.")
        _attach_file(msg, Path(auto))
        attachments.append(auto)

    # send SMTP
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ctx) as s:
            s.login(SENDER_EMAIL, SENDER_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        raise HTTPException(500, detail=f"SMTP send failed: {e}")

    return {"sent": True, "to": to, "cc": cc_list, "subject": subject, "attachments": attachments}
