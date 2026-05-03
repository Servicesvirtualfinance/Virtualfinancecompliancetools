# app/routers/mail_proposal.py
from __future__ import annotations
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, status
from typing import Optional, List, Dict, Any
from pathlib import Path
import json, re, smtplib, ssl, traceback, unicodedata
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText

# --- settings (reuse your existing constants) ---
try:
    # Use the working Gmail app-password config you mentioned earlier
    from app.settings import SENDER_EMAIL, SENDER_PASSWORD, SMTP_SERVER, SMTP_PORT, COMPANIES_ROOT
except Exception:
    # Sensible defaults if import path differs; change if needed
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT   = 465
    SENDER_EMAIL = "services@virtualfinanceservice.com"
    SENDER_PASSWORD = "CHANGE_ME"
    COMPANIES_ROOT = Path(__file__).resolve().parents[2] / "companies"

router = APIRouter(prefix="/api/mail/proposal", tags=["mail: proposal"])

# ---------- small utils ----------
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

def log(msg: str) -> None:
    print(f"[proposal_api] {msg}", flush=True)

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[^\w\s-]", "", s, flags=re.U)
    s = re.sub(r"[-\s]+", "-", s, flags=re.U).strip("-_").lower()
    return s

def safe_folderify(name: str) -> str:
    # keep original casing (your companies folders look like Title Case),
    # but remove forbidden characters for Windows paths.
    cleaned = re.sub(r'[<>:"/\\|?*]+', " ", name).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned

def find_latest_master(company_folder: Path) -> Optional[Path]:
    files = sorted(company_folder.glob("_master_*.json"))
    return files[-1] if files else None

def get_email_from_master(company_name: str) -> Optional[str]:
    folder = Path(COMPANIES_ROOT) / safe_folderify(company_name)
    if not folder.exists():
        return None
    master = find_latest_master(folder)
    if not master or not master.exists():
        return None
    try:
        data = json.loads(master.read_text(encoding="utf-8"))
        email = (data.get("email") or "").strip()
        if email and EMAIL_RX.fullmatch(email):
            return email
    except Exception:
        return None
    return None

def build_html(company_name: str, message: str) -> str:
    # Simple, client-safe HTML (same visual family as your onboarding emails)
    banner = "https://raw.githubusercontent.com/virtualfinance1992/vf-assets/main/ChatGPT%20Image%20Oct%2021%2C%202025%2C%2012_12_25%20AM.png"
    return f"""\
<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="x-apple-disable-message-reformatting"></head>
<body style="margin:0;padding:0;background:#f4f6f8;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f4f6f8;">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" width="640" style="max-width:640px;background:#ffffff;border-radius:12px;overflow:hidden;margin:24px;">
        <tr><td><img src="{banner}" alt="Virtual Finance" width="640" style="display:block;width:100%;height:220px;object-fit:cover;border:0;"></td></tr>
        <tr><td style="padding:20px 28px;font-family:Segoe UI,Roboto,Arial,sans-serif;color:#0f172a;">
          <h2 style="margin:0 0 8px 0;font-size:20px;">Proposal for {company_name}</h2>
          <p style="margin:0 0 12px 0;line-height:1.6;color:#334155;">
            {message}
          </p>
          <div style="margin:10px 0 0 0;font-size:13px;color:#64748b;">
            The detailed proposal PDF is attached with this email.
          </div>
        </td></tr>
        <tr><td align="center" style="background:#f1f1f1;font-size:12px;color:#666;padding:14px;font-family:Segoe UI,Roboto,Arial,sans-serif;">
          © 2025 Konar Virtual Fintech Services Pvt Ltd · Nerul, Navi Mumbai · +91 7738895510
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""

def send_email_with_attachment(
    to_email: str,
    subject: str,
    html_body: str,
    attachment_bytes: bytes,
    attachment_filename: str,
    cc: Optional[List[str]] = None,
) -> None:
    if not EMAIL_RX.fullmatch(to_email):
        raise HTTPException(status_code=400, detail="Recipient email looks invalid.")

    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_email
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    part = MIMEApplication(attachment_bytes, _subtype="pdf")
    part.add_header("Content-Disposition", "attachment", filename=attachment_filename)
    msg.attach(part)

    recipients = [to_email] + (cc or [])

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, recipients, msg.as_string())

# ---------- API ----------

@router.post("/send", status_code=status.HTTP_200_OK)
async def send_proposal(
    company_name: str = Form(...),
    # If frontend passes recipient, we use it; else we fallback to master.json
    to_email: Optional[str] = Form(None),
    subject: Optional[str] = Form(None),
    message: Optional[str] = Form("Please find attached our proposal. Feel free to reply if you need any changes."),
    cc: Optional[str] = Form(None),   # comma-separated
    proposal_pdf: UploadFile = File(...),  # must be a single PDF
):
    try:
        # resolve recipient
        if to_email:
            to_email = to_email.strip()
            if not EMAIL_RX.fullmatch(to_email):
                raise HTTPException(400, "Provided recipient email is invalid.")
        else:
            looked = get_email_from_master(company_name)
            if not looked:
                raise HTTPException(400, "Recipient email missing (not provided and not found in master).")
            to_email = looked

        # subject default
        if not subject or not subject.strip():
            subject = f"Proposal – {company_name}"

        # read attachment
        if proposal_pdf.content_type not in ("application/pdf", "application/octet-stream"):
            # some browsers send octet-stream; we still accept it
            log(f"Incoming content-type: {proposal_pdf.content_type}")
        data = await proposal_pdf.read()
        if not data:
            raise HTTPException(400, "Empty proposal file.")
        filename = proposal_pdf.filename or f"Proposal_{slugify(company_name)}.pdf"

        # build HTML + send
        html = build_html(company_name, message or "")
        cc_list = [e.strip() for e in (cc or "").split(",") if e.strip()] or None
        send_email_with_attachment(
            to_email=to_email,
            subject=subject.strip(),
            html_body=html,
            attachment_bytes=data,
            attachment_filename=filename,
            cc=cc_list,
        )

        log(f"Sent proposal to {to_email} (company={company_name}, file={filename})")
        return {"status": "SUCCESS", "to": to_email, "company": company_name, "filename": filename}

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        log(f"ERROR: {e!r}\n{tb}")
        raise HTTPException(status_code=500, detail=str(e))
