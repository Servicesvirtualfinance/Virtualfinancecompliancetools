# app/utils/render_docx.py
from pathlib import Path
from datetime import datetime, date as _date
from typing import Dict, Any, List, Optional
import json
import pandas as pd
from docxtpl import DocxTemplate

# --- ADD THIS helper near the top (below imports) ---
from glob import glob
import json

def _backfill_email_from_outputs(master_path: Path) -> str | None:
    """Look into outputs/mca_extract_*.json to backfill company email if master lacks it."""
    try:
        company_folder = master_path.parent
        outputs_dir = company_folder / "outputs"
        if not outputs_dir.exists():
            return None
        # pick latest mca_extract_*.json
        candidates = sorted(outputs_dir.glob("mca_extract_*.json"))
        if not candidates:
            return None
        latest = candidates[-1]
        log(f"Backfill: inspecting MCA extract: {latest}")
        data = json.loads(latest.read_text(encoding="utf-8"))
        # Try multiple shapes
        md = data.get("master_data", {})
        email = md.get("email")
        if email:
            return email
        emails_found = md.get("emails_found") or data.get("emails_found") or []
        if isinstance(emails_found, list) and emails_found:
            return emails_found[0]
    except Exception as e:
        log(f"Backfill: WARNING could not read MCA extract for email: {e!r}")
    return None

def log(msg: str):
    print(f"[render_docx] {msg}")

def _latest_master(company_folder: Path) -> Path:
    masters = sorted(company_folder.glob("_master_*.json"))
    if not masters:
        raise FileNotFoundError(f"No master found in: {company_folder}")
    return masters[-1]

def _pair_side_by_side(directors: List[Dict[str, Any]]):
    out, buf = [], []
    for d in directors or []:
        buf.append(d)
        if len(buf) == 2:
            out.append(buf)
            buf = []
    if buf:
        out.append([buf[0], None])
    return out

def _pick_email(m: Dict[str, Any]) -> Optional[str]:
    if m.get("email"):
        return m["email"]
    # support a few shapes we used earlier
    emails = m.get("master_data", {}).get("emails_found") or m.get("emails_found")
    return emails[0] if isinstance(emails, list) and emails else None

def infer_fy_from_date(d: _date) -> str:
    """Indian FY (Apr–Mar) string like '2025-26'."""
    y = d.year
    if d.month >= 4:
        return f"{y}-{str((y + 1) % 100).zfill(2)}"
    else:
        return f"{y - 1}-{str((y) % 100).zfill(2)}"

# --- REPLACE build_context(...) with this version ---
def build_context(mpath: Path, explicit_date: Optional[str] = None) -> Dict[str, Any]:
    log(f"Reading master JSON: {mpath}")
    m = json.loads(mpath.read_text(encoding="utf-8"))

    # Try several places for email in the master first
    def _pick_email(mdict: dict) -> Optional[str]:
        if mdict.get("email"):
            return mdict["email"]
        md = mdict.get("master_data", {})
        if md.get("email"):
            return md["email"]
        emails = md.get("emails_found") or mdict.get("emails_found")
        if isinstance(emails, list) and emails:
            return emails[0]
        return None

    email_in_master = _pick_email(m)
    if email_in_master:
        log(f"Email found in master: {email_in_master}")
    else:
        # Backfill from the latest MCA extract JSON in outputs/
        backfill = _backfill_email_from_outputs(mpath)
        if backfill:
            log(f"Email backfilled from MCA extract: {backfill}")
            email_in_master = backfill
        else:
            log("Email not found in master or MCA extract; leaving blank ('-').")

    company = {
        "name": m.get("company_name") or m.get("master_data", {}).get("company_name"),
        "cin": m.get("cin") or m.get("master_data", {}).get("cin"),
        "registered_address": m.get("registered_address") or m.get("master_data", {}).get("registered_address"),
        "email": email_in_master,
        "pan": m.get("pan"),
        "tan": m.get("tan"),
    }
    directors = m.get("directors") or m.get("master_data", {}).get("directors") or []
    today = datetime.now().strftime("%d-%m-%Y")
    ctx = {
        "today": today,
        "extra": {"date": explicit_date} if explicit_date else {},
        "company": company,
        "directors": directors,
        "directors_pairs": _pair_side_by_side(directors),
        "pan": {"pan_number": company["pan"]} if company.get("pan") else None,
    }
    log(f"Context primed: company={company['name']}, email={company.get('email') or '-'}," 
        f" directors={len(directors)}, date={ctx['extra'].get('date') or today}")
    return ctx


def _pick(df: pd.DataFrame, options: set[str]) -> Optional[str]:
    for col in df.columns:
        lc = str(col).strip().lower().replace(" ", "_")
        if lc in options or any(opt in lc for opt in options):
            return str(col)
    return None

def _normalize_auditor_columns(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    return {
        "auditor_id": _pick(df, {"auditor_id", "id", "auditor_code"}),
        "auditor_name": _pick(df, {"auditor_name", "name"}),
        "frn": _pick(df, {"frn", "firm_reg_no", "firm_registration_no", "firm_registration_number"}),
        "partner_name": _pick(df, {"partner_name"}),
        "membership_no": _pick(df, {"membership_no", "membership_number"}),
        "company_name": _pick(df, {"company_name", "company", "entity_name"}),
        "fy": _pick(df, {"fy", "financial_year", "financialyear", "f_y"}),
    }

def enrich_with_auditor(ctx: Dict[str, Any], auditors_xlsx: Path,
                        auditor_id: Optional[str], fy: Optional[str] = None) -> Dict[str, Any]:
    """Add auditor block + FY count to context safely with logs."""
    if not auditor_id:
        log("No auditor_id provided; skipping auditor enrichment.")
        return ctx
    if not auditors_xlsx.exists():
        raise FileNotFoundError(f"Auditors master not found: {auditors_xlsx}")

    log(f"Loading auditors master: {auditors_xlsx}")
    df = pd.read_excel(auditors_xlsx)
    log(f"Auditors columns: {list(df.columns)} (rows={len(df)})")

    cols = _normalize_auditor_columns(df)
    if not cols["auditor_id"] or not cols["auditor_name"]:
        raise ValueError("Auditors file missing required columns (need at least auditor_id and auditor_name)")

    # Locate the chosen auditor row (first match)
    mask_id = df[cols["auditor_id"]].astype(str) == str(auditor_id)
    if not mask_id.any():
        raise ValueError(f"Auditor '{auditor_id}' not found in {auditors_xlsx}")
    r = df.loc[mask_id].iloc[0]

    current_fy = fy or infer_fy_from_date(_date.today())
    total_for_fy = 0

    # Compute count safely, depending on available columns
    try:
        if cols["fy"] and cols["company_name"]:
            mask_fy = df[cols["fy"]].astype(str) == str(current_fy)
            total_for_fy = int(df[mask_id & mask_fy][cols["company_name"]].nunique())
            log(f"Counted companies for auditor_id={auditor_id} in FY={current_fy}: {total_for_fy}")
        elif cols["company_name"]:
            # No FY column → count across all rows for the auditor
            total_for_fy = int(df[mask_id][cols["company_name"]].nunique())
            log(f"FY column missing; counted companies across ALL FY for auditor_id={auditor_id}: {total_for_fy}")
        else:
            # No company_name either → just count rows
            total_for_fy = int(df[mask_id].shape[0])
            log(f"'company_name' column missing; counted rows for auditor_id={auditor_id}: {total_for_fy}")
    except Exception as e:
        log(f"WARNING: Failed counting companies for FY: {e!r}; defaulting to 0.")
        total_for_fy = 0

    ctx["auditor"] = {
        "id": str(r.get(cols["auditor_id"], "")),
        "name": str(r.get(cols["auditor_name"], "") or ""),
        "frn": str(r.get(cols["frn"], "") or "") if cols["frn"] else "",
        "partner_name": str(r.get(cols["partner_name"], "") or "") if cols["partner_name"] else "",
        "membership_no": str(r.get(cols["membership_no"], "") or "") if cols["membership_no"] else "",
        "total_companies_current_fy": total_for_fy,
    }
    log(f"Auditor set in context: name={ctx['auditor']['name']}, frn={ctx['auditor']['frn']}, total_fy={total_for_fy}")
    return ctx

def _xml_escape_value(v):
    if isinstance(v, str):
        # basic XML escapes to satisfy Word's header/footer XML
        return (v.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
    return v

def _xml_escape_ctx(x):
    if isinstance(x, dict):
        return {k: _xml_escape_ctx(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_xml_escape_ctx(v) for v in x]
    return _xml_escape_value(x)

def render_docx(template_path: Path, out_folder: Path, ctx: Dict[str, Any], template_id: str) -> Path:
    out_folder.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_folder / f"{template_id}_{ts}.docx"
    log(f"Rendering template='{template_path.name}' → '{out_path}'")
    try:
        log(f"Template context keys: {list(ctx.keys())}; company={ctx.get('company',{}).get('name')}")
    except Exception:
        pass

    # NEW: escape everything to prevent '&' (and '<' '>') from breaking header/footer XML
    safe_ctx = _xml_escape_ctx(ctx)

    tpl = DocxTemplate(str(template_path))
    tpl.render(safe_ctx)
    tpl.save(str(out_path))
    log(f"Saved document: {out_path}")
    return out_path


def enrich_with_auditor(ctx, master_path, auditor_id: str, fy=None):
    import pandas as pd
    df = pd.read_excel(master_path)

    # Normalize column names
    norm = {str(c).strip().lower().replace(" ", "_"): c for c in df.columns}

    def pick(*cands):
        for k in cands:
            if k in norm:
                return norm[k]
        return None

    # locate row by auditor_id (adjust key if needed)
    id_col = pick("auditor_id","id")
    row = df[df[id_col] == int(auditor_id)].iloc[0] if id_col else df.iloc[0]

    def g(*cands):
        c = pick(*cands)
        return "" if not c else ("" if pd.isna(row[c]) else str(row[c]))

    ctx.setdefault("auditor", {})
    ctx["auditor"]["name"]          = g("auditor_name","name")
    ctx["auditor"]["membership_no"] = g("membership_no","membership_number")
    ctx["auditor"]["frn"]           = g("frn","firm_reg_no","firm_registration_no","firm_registration_number")
    ctx["auditor"]["address"]       = g("address","office_address","firm_address","registered_address")  # <-- key line
    return ctx
