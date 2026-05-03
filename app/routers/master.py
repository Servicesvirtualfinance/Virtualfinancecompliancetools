# app/routers/master.py
from fastapi import APIRouter, UploadFile, File, HTTPException, status, Form
from pathlib import Path
import shutil, json, re, sys, traceback
from typing import Optional, List
from datetime import datetime
from ..settings import COMPANIES_ROOT, BACKEND_ROOT


# Ensure backend root on sys.path so we can import processes.*
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from processes.p01_mca_extract import run as mca_run
from processes.p02_pan_extract import run as pan_run
# OCR/text fallbacks
try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

try:
    from pdf2image import convert_from_path
    _HAS_PDF2IMG = True
except Exception:
    _HAS_PDF2IMG = False

try:
    import pytesseract
    _HAS_TESSERACT = True
except Exception:
    _HAS_TESSERACT = False

# NEW: pdfminer for parsing incorporation certificate (PAN/TAN)
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None  # we'll guard before using

router = APIRouter()  # ← unchanged (no prefix; include using prefix from main.py as before)

TMP_ROOT = (COMPANIES_ROOT.parent / "tmp")
TMP_ROOT.mkdir(parents=True, exist_ok=True)

def log(msg: str): print(f"[master_api] {msg}")

def safe_folderify(n: Optional[str]) -> str:
    s = re.sub(r'[<>:"/\\|?*]+', " ", (n or ""))
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or "UNKNOWN COMPANY"

def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:80] or "company"

def find_master_files(folder_company: Path) -> List[Path]:
    # accept both stable and timestamped master names
    return sorted(list(folder_company.glob("_master_*.json")))

# -------- helpers (added) ----------
def _apply_pan_number(master: dict, pan_number: Optional[str], origin: str) -> None:
    if not pan_number:
        return
    master.setdefault("pan", {})
    master["pan"]["pan_number"] = pan_number
    log(f"PAN set from {origin} -> {pan_number}")

PAN_RE        = re.compile(r"\b([A-Z]{5}\d{4}[A-Z])\b")
PAN_FUZZY_RE  = re.compile(r"\b([A-Z]\W*[A-Z]\W*[A-Z]\W*[A-Z]\W*[A-Z]\W*\d\W*\d\W*\d\W*\d\W*[A-Z])\b")
TAN_RE        = re.compile(r"\b([A-Z]{4}\d{5}[A-Z])\b")
TAN_FUZZY_RE  = re.compile(r"\b([A-Z]\W*[A-Z]\W*[A-Z]\W*[A-Z]\W*\d\W*\d\W*\d\W*\d\W*\d\W*[A-Z])\b")
# e.g., U12345MH2019PTC012345
CIN_RE        = re.compile(r"\b([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})\b", re.IGNORECASE)

def _normalize_fuzzy(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()

def _extract_text_pdfminer(pdf_path: str) -> str:
    if not pdf_extract_text:
        return ""
    try:
        return pdf_extract_text(pdf_path) or ""
    except Exception:
        return ""

def _extract_text_pypdf2(pdf_path: str) -> str:
    if not PdfReader:
        return ""
    try:
        r = PdfReader(pdf_path)
        parts = []
        for p in r.pages:
            try:
                parts.append(p.extract_text() or "")
            except Exception:
                pass
        return "\n".join(parts)
    except Exception:
        return ""

def _extract_text_ocr(pdf_path: str, dpi: int = 300) -> str:
    # Requires Poppler (for pdf2image) and Tesseract installed on Windows
    if not (_HAS_PDF2IMG and _HAS_TESSERACT):
        return ""
    try:
        pages = convert_from_path(pdf_path, dpi=dpi)
        texts = []
        for img in pages[:3]:  # COI is usually 1 page
            texts.append(pytesseract.image_to_string(img) or "")
        return "\n".join(texts)
    except Exception as e:
        log(f"WARN: OCR failed ({e!r}). Install Poppler & Tesseract or set tesseract_cmd.")
        return ""

def _search_ids(text: str) -> dict:
    out = {}
    T = text or ""

    # PAN strict then fuzzy-normalize
    m = PAN_RE.search(T)
    pan = m.group(1) if m else None
    if not pan:
        mf = PAN_FUZZY_RE.search(T)
        if mf:
            cand = _normalize_fuzzy(mf.group(1))
            pan = cand if PAN_RE.fullmatch(cand) else None
    if pan:
        out["pan"] = pan

    # TAN strict then fuzzy-normalize
    m = TAN_RE.search(T)
    tan = m.group(1) if m else None
    if not tan:
        mf = TAN_FUZZY_RE.search(T)
        if mf:
            cand = _normalize_fuzzy(mf.group(1))
            tan = cand if TAN_RE.fullmatch(cand) else None
    if tan:
        out["tan"] = tan

    # CIN (optional)
    m = CIN_RE.search(T)
    if m:
        out["cin"] = m.group(1).upper()

    # Company name (heuristic: line after “Name of the Company”)
    for line in T.splitlines():
        ll = line.lower()
        if "name of the company" in ll or "company name" in ll:
            parts = re.split(r"[:\-–]\s*", line, 1)
            if len(parts) == 2 and parts[1].strip():
                out["company_name"] = parts[1].strip(" .")
                break
    return out

def _parse_incorp_for_ids(pdf_path: Path) -> dict:
    """
    Robust PAN/TAN/CIN/Company Name extraction for COI:
    tries pdfminer → PyPDF2 → OCR; tolerates spaced PAN/TAN and normalizes.
    """
    # 1) pdfminer (text-based PDFs)
    t1 = _extract_text_pdfminer(str(pdf_path))
    # 2) PyPDF2 if text is too short (likely scanned/embedded)
    t2 = _extract_text_pypdf2(str(pdf_path)) if len(t1.strip()) < 40 else ""
    # 3) OCR if still nothing
    t3 = _extract_text_ocr(str(pdf_path))    if len((t1 + t2).strip()) < 40 else ""

    text = (t1 + "\n" + t2 + "\n" + t3).strip()
    if not text:
        log(f"WARN: No text extracted from {pdf_path.name}. This looks scanned; OCR deps missing?")
        return {}

    ids = _search_ids(text)
    if "pan" in ids: log(f"INCORP: PAN -> {ids['pan']}")
    if "tan" in ids: log(f"INCORP: TAN -> {ids['tan']}")
    if "cin" in ids: log(f"INCORP: CIN -> {ids['cin']}")
    if "company_name" in ids: log(f"INCORP: Company -> {ids['company_name']}")
    return ids

# -----------------------------------

@router.post("/generate", summary="Run MCA+PAN, rebuild master", status_code=status.HTTP_200_OK)
async def generate_master(
    mca_file: UploadFile = File(...),
    pan_file: Optional[UploadFile] = File(None),               # PAN optional (unchanged)
    incorp_file: Optional[UploadFile] = File(None),            # NEW: Incorporation Certificate (PDF)
    pan_number: Optional[str] = Form(None)                     # NEW: manual PAN override
):
    try:
        log(f"COMPANIES_ROOT = {COMPANIES_ROOT}")

        # -----------------------------
        # 1) Save incoming files
        # -----------------------------
        mca_path = TMP_ROOT / f"_upload_mca_{mca_file.filename}"
        with open(mca_path, "wb") as f:
            shutil.copyfileobj(mca_file.file, f)
        log(f"Saved MCA to {mca_path}")

        pan_res = {}
        if pan_file is not None:
            pan_path = TMP_ROOT / f"_upload_pan_{pan_file.filename}"
            with open(pan_path, "wb") as f:
                shutil.copyfileobj(pan_file.file, f)
            log(f"Saved PAN to {pan_path}")
            pan_res = pan_run(str(pan_path))  # creates/updates master
            log(f"PAN run complete: keys={list(pan_res.keys()) if isinstance(pan_res, dict) else type(pan_res)}")

        incorp_path = None
        if incorp_file is not None:
            incorp_path = TMP_ROOT / f"_upload_incorp_{incorp_file.filename}"
            with open(incorp_path, "wb") as f:
                shutil.copyfileobj(incorp_file.file, f)
            log(f"Saved INCORP to {incorp_path}")

        if pan_number:
            pan_number = pan_number.strip().upper()
            log(f"PAN number (form) = {pan_number}")

       # -----------------------------
        # 2) Run MCA
        # -----------------------------
        mca_res = mca_run(str(mca_path))
        log(f"MCA run complete: keys={list(mca_res.keys()) if isinstance(mca_res, dict) else type(mca_res)}")

        md, directors = {}, []
        email_from_mca = None  # <-- define before try!
        try:
            mca_json = (mca_res or {}).get("outputs", {}).get("json")
            if mca_json and Path(mca_json).exists():
                data = json.loads(Path(mca_json).read_text(encoding="utf-8"))
                md = data.get("master_data") or {}
                directors = data.get("directors") or []
                email_from_mca = (md.get("email") or md.get("company_email") or "").strip() or None
            if email_from_mca:
                log(f"Email (from MCA JSON) = {email_from_mca}")
            log(f"Loaded MCA details from JSON: directors={len(directors)} fields={len(md)}")
        except Exception as e:
            log(f"Failed reading MCA outputs JSON: {e!r} — using minimal fallbacks.")

        # use a single variable going forward
        resolved_email = email_from_mca

        # -----------------------------
        # 3) Resolve company & folder
        # -----------------------------
        company_name = (
            (pan_res.get("company_name") if isinstance(pan_res, dict) else None)
            or (mca_res.get("company_name") if isinstance(mca_res, dict) else None)
            or (md.get("company_name") if isinstance(md, dict) else None)
            or "UNKNOWN COMPANY"
        )
        folder_company = COMPANIES_ROOT / safe_folderify(company_name)
        folder_company.mkdir(parents=True, exist_ok=True)
        log(f"Company folder = {folder_company}")

        # -----------------------------
        # 4) Find or initialize master
        # -----------------------------
        masters = find_master_files(folder_company)
        log(f"Existing masters found: {len(masters)}")

        # Fallbacks if md is empty
        if not md:
            md = {
                "cin": (mca_res or {}).get("cin"),
                "company_name": company_name
            }
        if not directors:
            directors = []

        # === resolve PAN/TAN candidates BEFORE writing ===
        # precedence: user pan_number > pan_file parse > incorp pdf parse
        pan_from_form = pan_number or None
        pan_from_pan_file = None
        if isinstance(pan_res, dict):
            pan_from_pan_file = (pan_res.get("outputs") or {}).get("pan_number")
        pan_from_incorp = None
        tan_from_incorp = None
        if incorp_path:
            ids = _parse_incorp_for_ids(incorp_path)
            pan_from_incorp = ids.get("pan")
            tan_from_incorp = ids.get("tan")

        if not masters:
            # No master yet → initialize from MCA (MCA-first run allowed)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = slugify(company_name)
            master_path = folder_company / f"_master_{slug}_{ts}.json"
            master = {
                "company_name": company_name,
                "cin": md.get("cin"),
                "registered_address": md.get("registered_address"),
                "roc_name": md.get("roc_name"),
                "category_of_company": md.get("category_of_company"),
                "subcategory_of_company": md.get("subcategory_of_company"),
                "class_of_company": md.get("class_of_company"),
                "authorised_capital": md.get("authorised_capital"),
                "paid_up_capital": md.get("paid_up_capital"),
                "directors": directors,
                # ---- NEW (non-breaking) ----
                # keep PAN nested; TAN flat (additive fields)
                "pan": {},
                # -----------------------------
                "email": email_from_mca or None,

                "meta": {"created": ts, "last_updated": ts},
                "mismatches": {},
                "process_log": [{
                    "process": "INIT_FROM_MCA",
                    "ts": ts,
                    "inputs": {"mca_file": str(mca_path)},
                    "outputs": {"cin": md.get("cin"), "directors_count": len(directors)},
                    "status": "SUCCESS"
                }]
            }
            if master.get("email"):
                log(f"Master email set -> {master['email']}")

            # apply PAN/TAN (priority)
            if pan_from_form:
                _apply_pan_number(master, pan_from_form, "form")
            elif pan_from_pan_file:
                _apply_pan_number(master, pan_from_pan_file, "pan_file")
            elif pan_from_incorp:
                _apply_pan_number(master, pan_from_incorp, "incorp_pdf")

            if tan_from_incorp and not master.get("tan"):
                master["tan"] = tan_from_incorp
                log(f"TAN set from incorp_pdf -> {tan_from_incorp}")

            master_path.write_text(json.dumps(master, indent=2, ensure_ascii=False), encoding="utf-8")
            log(f"Master initialized from MCA: {master_path}")

        else:
            # Load latest master and merge MCA fields
            master_path = masters[-1]
            master = json.loads(master_path.read_text(encoding="utf-8"))
            log(f"Loaded master: {master_path}")

            master.setdefault("mismatches", {})
            master.setdefault("process_log", [])
            master.setdefault("meta", {})
            master["cin"]                    = md.get("cin") or master.get("cin")
            master["registered_address"]     = md.get("registered_address") or master.get("registered_address")
            master["roc_name"]               = md.get("roc_name") or master.get("roc_name")
            master["category_of_company"]    = md.get("category_of_company") or master.get("category_of_company")
            master["subcategory_of_company"] = md.get("subcategory_of_company") or master.get("subcategory_of_company")
            master["class_of_company"]       = md.get("class_of_company") or master.get("class_of_company")
            master["authorised_capital"]     = md.get("authorised_capital") or master.get("authorised_capital")
            master["paid_up_capital"]        = md.get("paid_up_capital") or master.get("paid_up_capital")
            master["directors"]              = directors or master.get("directors", [])
            master["meta"]["last_updated"]   = datetime.now().strftime("%Y%m%d_%H%M%S")
            if email_from_mca:
                master["email"] = email_from_mca
            # else keep whatever is already in master

            if master.get("email"):
                log(f"Master email -> {master['email']}")

            # ---- NEW: apply PAN/TAN priority (non-breaking) ----
            master.setdefault("pan", {})
            if pan_from_form:
                _apply_pan_number(master, pan_from_form, "form")
            elif not master["pan"].get("pan_number"):
                if pan_from_pan_file:
                    _apply_pan_number(master, pan_from_pan_file, "pan_file")
                elif pan_from_incorp:
                    _apply_pan_number(master, pan_from_incorp, "incorp_pdf")
            if tan_from_incorp and not master.get("tan"):
                master["tan"] = tan_from_incorp
                log(f"TAN set from incorp_pdf -> {tan_from_incorp}")
            # ----------------------------------------------------

            master["process_log"].append({
                "process": "MCA_IMPORT",
                "ts": master["meta"]["last_updated"],
                "inputs": {"mca_file": str(mca_path)},
                "outputs": {"cin": master.get("cin"), "directors_count": len(master["directors"])},
                "status": "SUCCESS"
            })
            master_path.write_text(json.dumps(master, indent=2, ensure_ascii=False), encoding="utf-8")
            log(f"Master updated with MCA: {master_path}")

        # Snapshot log
        if master.get("pan", {}).get("pan_number"):
            log(f"PAN in master -> {master['pan']['pan_number']}")
        if master.get("tan"):
            log(f"TAN in master -> {master['tan']}")

        return {
            "status": "SUCCESS",
            "message": "Master generated/updated",
            "company_folder": str(folder_company),
            "master_path": str(master_path),
            "mca_outputs": mca_res.get("outputs", {}) if isinstance(mca_res, dict) else {},
            "pan_outputs": pan_res.get("outputs", {}) if isinstance(pan_res, dict) else {}
        }

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        log(f"ERROR: {e!r}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e!s}")
