from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = APP_DIR.parent

# Folders
COMPANIES_ROOT = BACKEND_ROOT / "companies"
FRONTEND_ROOT  = BACKEND_ROOT / "frontend"

# Templates
TEMPLATES_WORD_ROOT = BACKEND_ROOT / "templates" / "word"
TEMPLATES_WORD_ROOT.mkdir(parents=True, exist_ok=True)

# Auditor master (Excel) – keep your file here
AUDITORS_MASTER_PATH = BACKEND_ROOT / "templates" / "auditors_master_final.xlsx"
EMAILS_ROOT    = FRONTEND_ROOT / "emails"


# --- SMTP / Email settings (no .env used) ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT   = 465

# Your Gmail address (the account that will send emails)
SENDER_EMAIL = "vinayakkonar@virtualfinanceservice.com"

# Paste the 16-character Gmail App Password. You may paste it with spaces;
# this code will strip spaces automatically.
SENDER_PASSWORD = "kxpzif sfrxiap keg".replace(" ", "")

