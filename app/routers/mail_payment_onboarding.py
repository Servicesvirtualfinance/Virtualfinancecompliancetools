from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from pathlib import Path
import json, datetime, re, smtplib, socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Import YOUR settings (names match your snippet)
from ..settings import (
    COMPANIES_ROOT, FRONTEND_ROOT,
    SMTP_SERVER, SMTP_PORT,
    SENDER_EMAIL, SENDER_PASSWORD
)

router = APIRouter(
    prefix="/api/mail/payment-onboarding",
    tags=["mail:payment_onboarding"]
)

# Templates live under frontend/emails
TEMPLATES_DIR: Path = FRONTEND_ROOT / "emails"


# -----------------------
# Helpers
# -----------------------
def latest_master_json(companies_root: Path | str, company_name: str) -> Optional[dict]:
    """Return latest _master_*.json under companies/<Company>/ as dict (or None)."""
    root = Path(companies_root)
    cdir = root / company_name
    if not cdir.exists():
        return None
    masters = sorted(cdir.glob("_master_*.json"), reverse=True)
    if not masters:
        return None
    try:
        return json.loads(masters[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def render_simple_tokens(tpl: str, ctx: dict) -> str:
    """Replace {{ key }} and dotted {{ a.b.c }} tokens (no logic)."""
    def get_val(path: str):
        cur = ctx
        for k in path.split('.'):
            if isinstance(cur, dict):
                cur = cur.get(k)
            else:
                return ""
            if cur is None:
                return ""
        return str(cur)
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", lambda m: get_val(m.group(1).strip()), tpl)


def _is_email(s: Optional[str]) -> bool:
    """Very light email sanity check (no extra deps)."""
    if not s:
        return False
    s = s.strip()
    if "@" not in s:
        return False
    local, _, domain = s.partition("@")
    return bool(local) and "." in domain


def _get_nested(d: dict, dotted: str) -> Optional[str]:
    cur = d or {}
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur if isinstance(cur, str) else None


def resolve_recipient_email(master: dict | None, override: Optional[str]) -> Optional[str]:
    """Prefer explicit override; else try common master paths (root `email` first)."""
    if override and _is_email(override):
        return override
    candidates: List[str] = []
    if master:
        for path in [
            "email",                    # your master JSON uses this (root)
            "company.email",
            "contacts.primary.email",
            "contacts.billing.email",
            "contacts.info.email",
        ]:
            val = master.get(path) if "." not in path else _get_nested(master, path)
            if val and _is_email(val):
                candidates.append(val)
    return candidates[0] if candidates else None


def send_email_html(to_email: str, subject: str, html: str, cc: List[str] | None = None):
    """Send via Gmail SMTP SSL (port 465)."""
    cc = [c for c in (cc or []) if c]
    msg = MIMEMultipart('alternative')
    msg['From'] = SENDER_EMAIL
    msg['To'] = to_email
    if cc:
        msg['Cc'] = ", ".join(cc)
    msg['Subject'] = subject
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    recipients = [to_email] + cc

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=20) as s:
            s.login(SENDER_EMAIL, SENDER_PASSWORD)
            s.sendmail(SENDER_EMAIL, recipients, msg.as_string())
    except (socket.gaierror, TimeoutError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"SMTP connection failed: host={SMTP_SERVER}:{SMTP_PORT} ({e})"
        )
    except smtplib.SMTPAuthenticationError as e:
        raise HTTPException(
            status_code=401,
            detail=f"SMTP auth failed for {SENDER_EMAIL}: {str(e)}"
        )
    except smtplib.SMTPException as e:
        raise HTTPException(
            status_code=502,
            detail=f"SMTP error: {str(e)}"
        )


# -----------------------
# Request model (no EmailStr)
# -----------------------
class SendPaymentOnboardingIn(BaseModel):
    company_name: str = Field(..., description="Exact folder name under companies/")
    contact_email: Optional[str] = None            # resolve from master if missing
    contact_name: Optional[str] = None
    assigned_manager: Optional[str] = None
    plan_name: str
    amount: float
    currency: str = "INR"
    payment_id: str
    payment_date: str                               # ISO string from UI
    template: str = "email_payment_welcome.html"    # lives in frontend/emails/
    cc: Optional[str] = None


# -----------------------
# Endpoint
# -----------------------
@router.post("/send")
def send_payment_onboarding(payload: SendPaymentOnboardingIn):
    # 1) Resolve recipient (prefer payload; else master email locations)
    master = latest_master_json(COMPANIES_ROOT, payload.company_name)
    to_email = resolve_recipient_email(master, payload.contact_email)
    if not to_email:
        raise HTTPException(400, "Recipient email missing or invalid (not provided and not found in master).")

    # 2) Load HTML template
    tpath = TEMPLATES_DIR / payload.template
    if not tpath.exists():
        raise HTTPException(404, f"Template not found: {payload.template}")
    tpl = tpath.read_text(encoding="utf-8")

    # 3) Build context with safe defaults
    ctx = {
        "company": {"name": payload.company_name},
        "contact_name": payload.contact_name or "there",
        "assigned_manager": payload.assigned_manager or "Virtual Finance Team",
        "plan_name": payload.plan_name,
        "amount": f"{payload.amount:.2f}".rstrip('0').rstrip('.'),
        "currency": payload.currency,
        "payment_id": payload.payment_id,
        "payment_date": payload.payment_date,
        "next_steps_url": "https://vfoffice.in/welcome",
    }

    # 4) Render tokens -> HTML
    html = render_simple_tokens(tpl, ctx)

    # 5) Optional CC
    cc_list: List[str] | None = None
    if payload.cc and _is_email(payload.cc):
        cc_list = [payload.cc]

    # 6) Send
    subject = f"Payment Confirmation & Onboarding — {payload.company_name}"
    send_email_html(to_email, subject, html, cc=cc_list)

    # 7) Persist under companies/<Company>/emails/
    cdir = Path(COMPANIES_ROOT) / payload.company_name / "emails"
    cdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    (cdir / f"{ts}_payment_onboarding.html").write_text(html, encoding="utf-8")
    (cdir / f"{ts}_payment_onboarding.json").write_text(
        json.dumps({"to": to_email, "cc": (cc_list or []), "subject": subject, "ctx": ctx}, indent=2),
        encoding="utf-8"
    )

    return {"ok": True, "to": to_email}
