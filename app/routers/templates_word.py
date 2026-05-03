from fastapi import APIRouter, UploadFile, File, Form, Body, HTTPException
from pathlib import Path
import shutil
from typing import List, Optional
import pandas as pd

from ..settings import TEMPLATES_WORD_ROOT, COMPANIES_ROOT, AUDITORS_MASTER_PATH
from ..utils.render_docx import _latest_master, build_context, enrich_with_auditor, render_docx

router = APIRouter()
def log(msg: str): print(f"[templates_word] {msg}")

# ---------- NEW: List auditors for dropdown ----------
@router.get("/auditors", summary="List auditors from auditors_master_final.xlsx")
def list_auditors() -> List[dict]:
    if not AUDITORS_MASTER_PATH.exists():
        raise HTTPException(404, f"Auditors master not found at {AUDITORS_MASTER_PATH}")
    try:
        df = pd.read_excel(AUDITORS_MASTER_PATH)
    except Exception as e:
        raise HTTPException(500, f"Failed to read auditors file: {e}")

    def pick(options: set[str]) -> Optional[str]:
        for col in df.columns:
            lc = str(col).strip().lower().replace(" ", "_")
            if lc in options or any(opt in lc for opt in options):
                return str(col)
        return None

    col_id   = pick({"auditor_id","id","auditor_code"})
    col_name = pick({"auditor_name","name"})
    col_frn  = pick({"frn","firm_reg_no","firm_registration_no","firm_registration_number"})
    col_pn   = pick({"partner_name"})
    col_mno  = pick({"membership_no","membership_number"})
    col_addr = pick({"address","office_address","firm_address","registered_address"})  # <-- add this

    if not col_id or not col_name:
        raise HTTPException(500, "Required columns not found (need at least auditor_id and auditor_name)")

    items: List[dict] = []
    for _, row in df.iterrows():
        items.append({
            "auditor_id":    str(row.get(col_id, "") or ""),
            "auditor_name":  str(row.get(col_name, "") or ""),
            "frn":           ("" if not col_frn else str(row.get(col_frn, "") or "")),
            "partner_name":  ("" if not col_pn  else str(row.get(col_pn, "")  or "")),
            "membership_no": ("" if not col_mno else str(row.get(col_mno, "") or "")),
            "address": ("" if not col_addr else str(row.get(col_addr, "") or "")),            # <-- add this
        })

    log(f"Auditors listed: {len(items)}")
    return items

# ---------- Upload a .docx template (optional) ----------
@router.post("/upload", summary="Upload a Word (.docx) template")
async def upload_template(
    file: UploadFile = File(...),
    template_id: Optional[str] = Form(None)
):
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(400, "Only .docx files are accepted")

    stem = (template_id or Path(file.filename).stem).strip().replace(" ", "_")
    dest = TEMPLATES_WORD_ROOT / f"{stem}.docx"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    log(f"Saved template: {dest}")
    return {"status": "SUCCESS", "template_id": stem, "path": str(dest)}

# ---------- List available .docx templates ----------
@router.get("", summary="List available Word templates")
def list_templates() -> List[dict]:
    items = [{"id": p.stem, "file": p.name, "path": str(p)} for p in sorted(TEMPLATES_WORD_ROOT.glob("*.docx"))]
    log(f"Templates listed: {len(items)}")
    return items

# ---------- Render template to company folder (this creates the BOD doc) ----------
@router.post("/render", summary="Render template for a company")
def render_for_company(
    company_name: str = Body(...),
    template_id: str  = Body(...),          # e.g. "bod_auditor_first_appointment"
    date: Optional[str] = Body(None),
    auditor_id: Optional[str] = Body(None), # optional, enrich from auditors master
    fy: Optional[str] = Body(None)          # optional override FY for counts
):
    company_folder = COMPANIES_ROOT / company_name
    if not company_folder.exists():
        raise HTTPException(404, f"Company folder not found: {company_folder}")

    template_path = TEMPLATES_WORD_ROOT / f"{template_id}.docx"
    if not template_path.exists():
        raise HTTPException(404, f"Template not found: {template_path}")

    mpath = _latest_master(company_folder)
    log(f"Using master: {mpath}")

    ctx = build_context(mpath, explicit_date=date)
    if auditor_id:
        ctx = enrich_with_auditor(ctx, AUDITORS_MASTER_PATH, auditor_id=auditor_id, fy=fy)

    out_path = render_docx(template_path, company_folder, ctx, template_id)

    # Build a browser-friendly URL to the file (served by StaticFiles mount at /companies)
    try:
        # /companies/<Company>/<file>
        rel_url = f"/companies/{company_folder.name}/{Path(out_path).name}"
    except Exception:
        rel_url = ""

    return {
        "status": "SUCCESS",
        "company": company_name,
        "template_id": template_id,
        "output_docx": str(out_path),  # absolute path on disk
        "download_url": rel_url        # web path you can open in a new tab
    }



# --- ADD BELOW: list & exists of rendered docs for a company ---
from fastapi import HTTPException, Query
from pathlib import Path
from typing import Dict, List
from datetime import datetime
import re

from ..settings import COMPANIES_ROOT

# If your router is defined earlier as `router = APIRouter(prefix="/api/templates/word", tags=[...])`
# then these will be available at:
#   GET /api/templates/word/exists?company_name=...
#   GET /api/templates/word/list?company_name=...
# --- at the top ---
# from fastapi import APIRouter, ...
 # <- add prefix + tags


# Map of known template_ids to the filename prefixes you save with.
# --- in TEMPLATE_PREFIX mapping ---
TEMPLATE_PREFIX: Dict[str, str] = {
    "bod_auditor_first_appointment": "bod_auditor_first_appointment",
    "intimation_first_auditor_TEMPLATE": "intimation_first_auditor_TEMPLATE",
    "proposal_first_auditor_TEMPLATE": "proposal_first_auditor_TEMPLATE",
    "auditor_consent_TEMPLATE": "auditor_consent_TEMPLATE",
    "company_letterhead_TEMPLATE": "company_letterhead_TEMPLATE",
    "sla_agreement_TEMPLATE": "sla_agreement_TEMPLATE",  # <-- add this line
}


def _download_url(abs_path: Path) -> str:
    # Build a static URL that matches the /companies mount in main.py
    rel = abs_path.relative_to(COMPANIES_ROOT).as_posix()
    return f"/companies/{rel}"

def _parse_ts_from_name(p: Path) -> str:
    # Extract timestamp like _YYYYMMDD_HHMMSS.docx -> "YYYY-MM-DD HH:MM:SS"
    m = re.search(r"_(\d{8})_(\d{6})\.docx$", p.name)
    if not m:
        return ""
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

@router.get("/exists", summary="Which rendered docs exist for a company")
def docs_exist(company_name: str = Query(..., description="Exact company folder name")) -> Dict[str, bool]:
    folder = COMPANIES_ROOT / company_name
    if not folder.exists():
        raise HTTPException(status_code=404, detail="Company folder not found")

    out: Dict[str, bool] = {}
    for tid, prefix in TEMPLATE_PREFIX.items():
        out[tid] = any(folder.glob(f"{prefix}_*.docx"))
    return out

@router.get("/list", summary="List rendered docs for a company")
def list_docs(company_name: str = Query(..., description="Exact company folder name")) -> List[dict]:
    folder = COMPANIES_ROOT / company_name
    if not folder.exists():
        raise HTTPException(status_code=404, detail="Company folder not found")

    rows: List[dict] = []
    for tid, prefix in TEMPLATE_PREFIX.items():
        for p in folder.glob(f"{prefix}_*.docx"):
            rows.append({
                "template_id": tid,
                "filename": p.name,
                "download_url": _download_url(p),
                "timestamp": _parse_ts_from_name(p),
            })
    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return rows
