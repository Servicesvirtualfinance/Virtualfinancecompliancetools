# app/routers/inc20a.py
from __future__ import annotations

import io, json, re, time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Body, Query, UploadFile, File, Form
from pydantic import BaseModel

from ..settings import BACKEND_ROOT, COMPANIES_ROOT
from ..utils.render_docx import _latest_master, build_context, render_docx

# ===== Optional parsers (gracefully degrade if missing) =====
try:
    import pdfplumber  # pip install pdfplumber
except Exception:
    pdfplumber = None

try:
    import PyPDF2  # pip install PyPDF2
except Exception:
    PyPDF2 = None

# app/routers/inc20a.py (imports section)
try:
    import pytesseract
    from PIL import Image
    from pathlib import Path  # already imported above; keep if present
    TESS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if Path(TESS_PATH).exists():
        pytesseract.pytesseract.tesseract_cmd = TESS_PATH
except Exception:
    pytesseract = None
    Image = None


# Extra extractors (all optional)
try:
    # pip install pdfminer.six
    from pdfminer.high_level import extract_text as pdfminer_extract_text
except Exception:
    pdfminer_extract_text = None

try:
    # pip install easyocr
    import easyocr
except Exception:
    easyocr = None

try:
    # pip install PyMuPDF
    import fitz  # PyMuPDF
except Exception:
    fitz = None

# === If Tesseract is installed in a custom path, you can hard-set it here:
# if pytesseract:
#     pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ===== Router =====
router = APIRouter(prefix="/api/inc20a", tags=["inc20a"])

# ===== Config / constants =====
TEMPLATES_ROOT = BACKEND_ROOT / "templates" / "inc20a"
PackKey = Literal["opc", "private_limited", "section8"]

NUM_IND = r"(?:\d{1,3}(?:,\d{2}){1,}|\d{1,3}(?:,\d{3})+|[0-9][0-9,]*)"  # Indian/intl digits

# ===== Misc helpers =====
def _log(msg: str) -> None:
    print(f"[inc20a] {msg}")

def _safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", s.strip()).strip("_").lower()

def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def _company_folder(name: str) -> Path:
    p = COMPANIES_ROOT / name
    if not p.exists():
        raise HTTPException(404, f"Company folder not found: {p}")
    return p

def company_inc20a_dir(company: str) -> Path:
    base = _company_folder(company) / "INC20A"
    (base / "inputs" / "bank").mkdir(parents=True, exist_ok=True)
    # NOTE: extracted artifacts live directly in base (per your request)
    base.mkdir(parents=True, exist_ok=True)
    return base

def newest(paths: List[Path]) -> Optional[Path]:
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None

def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

def latest_json(company: str, prefix: str) -> Optional[dict]:
    incdir = company_inc20a_dir(company)  # look directly under INC20A (no subfolder)
    files = sorted(incdir.glob(f"{prefix}_*.json"))
    if not files:
        return None
    return read_json(files[-1])

def _download_url(abs_path: Path) -> str:
    rel = abs_path.relative_to(COMPANIES_ROOT).as_posix()
    return f"/companies/{rel}"

# ===== Pack handling =====
def _find_pack_dir(pack: PackKey) -> Path:
    """
    Tolerant folder matching:
      - 'opc'              -> 'opc' / 'OPC' ...
      - 'private_limited'  -> 'private', 'pvt', 'privatelimited'
      - 'section8'         -> 'section8', 'section 8', 'sec8'
    """
    if not TEMPLATES_ROOT.exists():
        raise HTTPException(404, f"INC20A templates root not found: {TEMPLATES_ROOT}")

    want = {
        "opc": ["opc"],
        "private_limited": ["private", "pvt", "privatelimited"],
        "section8": ["section8", "section 8", "sec8"],
    }[pack]

    for d in TEMPLATES_ROOT.iterdir():
        if d.is_dir():
            name = d.name.replace("-", "").replace(" ", "").lower()
            if any(w.replace(" ", "") in name for w in want):
                return d

    fallback = {
        "opc": TEMPLATES_ROOT / "OPC",
        "private_limited": TEMPLATES_ROOT / "Private",
        "section8": TEMPLATES_ROOT / "Section8",
    }[pack]
    if fallback.exists():
        return fallback

    raise HTTPException(404, f"No folder for pack='{pack}' under {TEMPLATES_ROOT}")

def _list_docx(dirpath: Path) -> List[Path]:
    # Ignore Word lock/temp files: "~$*.docx", "~*.docx", etc.
    files: List[Path] = []
    for p in dirpath.glob("*.docx"):
        name, stem = p.name, p.stem
        if name.startswith(("~$", "~")) or stem.startswith(("~$", "~")):
            continue
        if name.lower().endswith((".tmp.docx", ".bak.docx")):
            continue
        files.append(p)
    return sorted(files)

# ===== MOA (INC-33) ingest & parse =====
def find_moa_pdf(company: str) -> Optional[Path]:
    """Find MOA PDF with tolerant, case-insensitive matching in company root and INC20A."""
    cdir = _company_folder(company)
    candidates: List[Path] = []

    # Common patterns
    pats = [
        "INC-33*_MOA*.pdf", "*_MOA*.pdf", "*INC33*MOA*.pdf",
        "INC 33*_MOA*.pdf", "MOA*.pdf", "*INC-33*.pdf", "*INC 33*.pdf"
    ]
    for base in (cdir, cdir / "INC20A"):
        for pat in pats:
            candidates += list(base.glob(pat))

    if candidates:
        return newest(candidates)

    # Last-resort scan across all PDFs
    all_pdfs = list(cdir.glob("*.pdf")) + list((cdir / "INC20A").glob("*.pdf"))
    best = None
    for p in all_pdfs:
        n = p.name.lower()
        if "moa" in n or "inc-33" in n or "inc 33" in n or "inc33" in n:
            best = p if (best is None or p.stat().st_mtime > best.stat().st_mtime) else best
    return best

def pdf_to_text(pdf_path: Path) -> str:
    # 0) PyMuPDF get_text (often best for MCA generated PDFs)
    txt = _pymupdf_text(pdf_path)
    if txt.strip():
        return txt

    # 1) pdfplumber
    if pdfplumber:
        try:
            chunks: List[str] = []
            with pdfplumber.open(str(pdf_path)) as pdf:
                for page in pdf.pages:
                    chunks.append(page.extract_text() or "")
            txt = "\n".join(chunks)
            if txt.strip():
                return txt
        except Exception as e:
            _log(f"pdfplumber failed: {e}")

    # 2) PyPDF2
    if PyPDF2:
        try:
            with open(pdf_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                txt = "\n".join([(p.extract_text() or "") for p in reader.pages])
                if txt.strip():
                    return txt
        except Exception as e:
            _log(f"PyPDF2 failed: {e}")

    # 3) pdfminer.six
    if pdfminer_extract_text:
        try:
            txt = pdfminer_extract_text(str(pdf_path)) or ""
            if txt.strip():
                return txt
        except Exception as e:
            _log(f"pdfminer.six failed: {e}")

    return ""


def parse_moa_text(txt: str, form: Optional[Dict[str, str]] = None) -> dict:
    up = (txt or "").upper()
    cleaned = re.sub(r"[ \t]+", " ", up)
    out: dict = {
        "company_name": None,
        "state": None,
        "share_capital": {"total_amount": None, "no_of_shares": None, "face_value": None},
        "subscribers": [],
    }

    # ---- 1) Company name: prefer form value; else block "1 The name of the company is ..."
    # Try common form field keys
    if form:
        for k, v in form.items():
            kl = k.lower()
            if any(t in kl for t in ["nameofthecompany", "companyname", "name_of_company"]):
                if v.strip():
                    out["company_name"] = v.strip().upper()
                    break
    if not out["company_name"]:
        m = re.search(r"1\s+THE NAME OF THE COMPANY IS\s+([A-Z0-9 &\.\-]+?)(?:\s+2\b|REGISTERED|TABLE|A -)", cleaned, re.S)
        if m:
            out["company_name"] = m.group(1).strip(" .")

    # ---- 2) Registered office state
    if form and not out["state"]:
        for k, v in form.items():
            if "state" in k.lower() and v.strip():
                out["state"] = v.strip().upper()
                break
    if not out["state"]:
        m = re.search(r"REGISTERED OFFICE.*?STATE OF\s*([A-Z ]+)", cleaned, re.S)
        if m:
            out["state"] = m.group(1).strip()

    # ---- 3) Share capital:
    # (a) Text line: "The share capital of the company is 1500000 rupees"
    m_total = re.search(r"THE SHARE CAPITAL OF THE COMPANY IS\s*([0-9][0-9,]*)\s*RUPEES", cleaned)
    if m_total:
        out["share_capital"]["total_amount"] = int(re.sub(r"[^\d]", "", m_total.group(1)))

    # (b) Table line: "1500000 Equity Share Shares of 1 Rupees each"
    m_break = re.search(r"\b([0-9][0-9,]*)\s+EQUITY\s+SHARE\S*\s+SHARES\s+OF\s+([0-9][0-9,]*)\s+RUPEES\s+EACH", cleaned)
    if m_break:
        shares = int(re.sub(r"[^\d]", "", m_break.group(1)))
        face = int(re.sub(r"[^\d]", "", m_break.group(2)))
        out["share_capital"]["no_of_shares"] = shares
        out["share_capital"]["face_value"] = face
        if not out["share_capital"]["total_amount"]:
            out["share_capital"]["total_amount"] = shares * face

    # (c) If only total and face are present, compute shares
    sc = out["share_capital"]
    if sc["total_amount"] and sc["face_value"] and not sc["no_of_shares"]:
        sc["no_of_shares"] = sc["total_amount"] // sc["face_value"]

    # ---- 4) Subscribers:
    # Focus on block after "SUBSCRIBER DETAILS"
    subs: List[dict] = []
    if "SUBSCRIBER DETAILS" in cleaned:
        block = cleaned.split("SUBSCRIBER DETAILS", 1)[1]
        # capture "NAME ... <PAN/DIN> ... 100000 EQUITY"
        for m in re.finditer(r"\n([A-Z][A-Z \.',/-]{3,}?)\n.*?\b([A-Z*\d]{5,})\b.*?\b([0-9][0-9,]{2,})\s+EQUITY", block, re.S):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .,/")
            shares = int(re.sub(r"[^\d]", "", m.group(3)))
            subs.append({"name": name, "equity_shares": shares})
    # de-dup
    seen = set()
    dedup = []
    for s in subs:
        key = (s["name"], s["equity_shares"])
        if key not in seen:
            seen.add(key)
            dedup.append(s)
    out["subscribers"] = dedup
    return out


# ===== Bank OCR & parse =====
IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
ACCT_RE = re.compile(r"\b(?:A/C|ACCOUNT)\s*(?:NO\.?|NUMBER)?\s*[:\-]?\s*([0-9X]{6,18})\b", re.I)
AMT_RE = re.compile(r"(\d{1,3}(?:,\d{2})+|\d{1,3}(?:,\d{3})+|\d+(?:\.\d{2})?)")

def _read_pdf_pages_as_images(pdf_path: Path) -> List["Image.Image"]:
    """Use PyMuPDF to rasterize a PDF into PIL images (optional)."""
    imgs: List["Image.Image"] = []
    if not fitz or not Image:
        return imgs
    try:
        doc = fitz.open(str(pdf_path))
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            pil_img = Image.open(io.BytesIO(pix.tobytes("png")))
            imgs.append(pil_img)
    except Exception as e:
        _log(f"PDF rasterize failed: {e}")
    return imgs

def ocr_image_to_text(img_path: Path) -> str:
    ext = img_path.suffix.lower()

    # ----- 1) EasyOCR (no system install), best effort -----
    if easyocr:
        try:
            reader = easyocr.Reader(['en'], gpu=False)
            if ext == ".pdf":
                # Rasterize PDF pages and OCR each
                texts: List[str] = []
                imgs = _read_pdf_pages_as_images(img_path)
                if imgs:
                    for pil_img in imgs:
                        try:
                            import numpy as np  # optional
                            arr = np.array(pil_img)
                            results = reader.readtext(arr, detail=0, paragraph=True)
                        except Exception:
                            # If numpy not available, write temp PNG
                            tmp = img_path.with_suffix(".page.png")
                            pil_img.save(tmp)
                            results = reader.readtext(str(tmp), detail=0, paragraph=True)
                            try:
                                tmp.unlink(missing_ok=True)
                            except Exception:
                                pass
                        texts.append("\n".join(results))
                    text = "\n\n".join(texts).strip()
                    if text:
                        return text
            else:
                results = reader.readtext(str(img_path), detail=0, paragraph=True)
                text = "\n".join(results).strip()
                if text:
                    return text
        except Exception as e:
            _log(f"EasyOCR failed: {e}")

    # ----- 2) pytesseract fallback (requires system tesseract) -----
    if pytesseract and Image:
        try:
            if ext == ".pdf" and fitz:
                texts: List[str] = []
                for pil_img in _read_pdf_pages_as_images(img_path):
                    gray = pil_img.convert("L")
                    texts.append(pytesseract.image_to_string(gray))
                text = "\n\n".join(texts).strip()
                if text:
                    return text
            else:
                img = Image.open(str(img_path)).convert("L")
                text = pytesseract.image_to_string(img)
                if text:
                    return text
        except Exception as e:
            _log(f"OCR failed: {e}")

    return ""

def parse_bank_text(txt: str) -> dict:
    up = _fix_ocr_zeros((txt or "").upper())
    bank = {
        "name": None, "branch": None, "branch_code": None,
        "account_no": None, "ifsc": None, "account_open_date": None
    }

    # Bank name
    for candidate in ["STATE BANK OF INDIA", "SBI", "HDFC BANK", "ICICI BANK", "AXIS BANK", "BANK OF BARODA"]:
        if candidate in up:
            bank["name"] = "STATE BANK OF INDIA" if "SBI" in candidate or "STATE BANK OF INDIA" in candidate else candidate
            break

    # Branch code / branch
    m = re.search(r"BRANCH\s*CODE\s*[:\-]?\s*([0-9O]{3,5})", up)
    if m:
        bank["branch_code"] = _fix_ocr_zeros(m.group(1))
    m = re.search(r"BRANCH\s*EMAIL\s*[:\-]?\s*([A-Z0-9._%+-]+@[A-Z0-9.-]+)", up)
    if not m:
        m = re.search(r"BRANCH\s*[:\-]?\s*([A-Z0-9 \-/().]{3,})", up)
    if m and not bank["branch"]:
        bank["branch"] = m.group(1).strip(" :/")

    # IFSC (fix O→0 etc)
    mif = re.search(r"\b([A-Z]{4}[0O][A-Z0-9]{6})\b", up)
    if mif:
        bank["ifsc"] = normalize_ifsc(mif.group(1))

    # Account number (tolerant)
    mac = re.search(r"(?:A/C|ACCOUNT)\s*(?:NO\.?|NUMBER)?\s*[:\-]?\s*([0-9XO ]{6,20})", up)
    if mac:
        bank["account_no"] = re.sub(r"[^0-9X]", "", mac.group(1))

    # Account open date
    m = re.search(r"ACCOUNT\s*OPEN\s*DATE\s*[:\-]?\s*(\d{2}[-/]\d{2}[-/]\d{4})", up)
    if m:
        bank["account_open_date"] = m.group(1)

    # Credits (grab amount using robust parser)
    credits: List[dict] = []
    for line in up.splitlines():
        if any(k in line for k in ["CHQ TRFR", "DEP TFR", "UPI", "NEFT", "IMPS", "RTGS"]):
            amt = parse_amount_any(line)
            name = None
            m_party = re.search(r"OF MR\.?\s*([A-Z ]{3,})", line) or re.search(r"OF\s+([A-Z ]{3,})", line) \
                      or re.search(r"UPI[/ ]([A-Z ]{3,})", line)
            if m_party:
                name = re.sub(r"\s+", " ", m_party.group(1)).strip(" /.")
            ref = None
            m_ref = re.search(r"\bREF(?:ERENCE)?[:\s]*([A-Z0-9-]{4,})", line) or re.search(r"\b([0-9]{6,})\b", line)
            if m_ref:
                ref = m_ref.group(1)
            credits.append({"narration": line[:180], "amount": amt, "party_name": name, "ref": ref})

    return {"bank": bank, "credits": credits}


# ===== Internal merge for autofill =====
def get_autofill_data(company: str) -> dict:
    moa = latest_json(company, "moa_parsed")
    bankp = latest_json(company, "bank_parsed")
    data: dict = {}

    if moa:
        data["moa"] = moa.get("parsed", {})
    if bankp:
        parsed = bankp.get("parsed", {})
        if "bank" in parsed:
            data["bank"] = parsed["bank"]
        if "credits" in parsed:
            data["credits"] = parsed["credits"]

    return data

# ---- PDF form fields (AcroForm) ----
def pdf_form_values(pdf_path: Path) -> Dict[str, str]:
    vals: Dict[str, str] = {}
    if not PyPDF2:
        return vals
    try:
        with open(pdf_path, "rb") as f:
            r = PyPDF2.PdfReader(f)
            # PyPDF2 >=3
            if hasattr(r, "get_form_text_fields"):
                raw = r.get_form_text_fields() or {}
                for k, v in raw.items():
                    if v is not None:
                        vals[str(k)].strip() if isinstance(v, str) else None
                return {k: (v or "").strip() for k, v in raw.items()}
            # Fallback for older
            fields = r.get_fields() or {}
            for k, field in fields.items():
                v = field.get("/V")
                if v is None:
                    continue
                try:
                    v = v.get_object()
                except Exception:
                    pass
                vals[str(k)] = str(v).strip()
    except Exception as e:
        _log(f"form read fail: {e}")
    return vals

# ---- Text extraction: add PyMuPDF get_text() try ----
def _pymupdf_text(pdf_path: Path) -> str:
    if not fitz:
        return ""
    try:
        txts = []
        doc = fitz.open(str(pdf_path))
        for page in doc:
            txts.append(page.get_text("text") or "")
        return "\n".join(txts)
    except Exception as e:
        _log(f"PyMuPDF get_text failed: {e}")
        return ""

# ---- OCR/Bank normalization helpers ----
def _fix_ocr_zeros(s: str) -> str:
    # Common OCR: O ↔ 0 inside mostly-numeric tokens
    return re.sub(r"(?<=\w)[O](?=\d)|(?<=\d)[O](?=\w)|(?<=\d)O(?=\d)", "0", s)

def normalize_ifsc(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = _fix_ocr_zeros(raw.upper().replace(" ", ""))
    s = s.replace("SBINOOO", "SBIN000")  # very common SBI OCR error
    # ensure 5th char is '0'
    if len(s) >= 5:
        s = s[:4] + "0" + s[5:]
    # final sanity
    m = re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", s)
    return s if m else raw

_AMT_INR_DEC_LAST2 = re.compile(r"\b(\d{1,3}(?:,\d{2}){2,})\b")          # e.g. 1,01,000,00
_AMT_WEST = re.compile(r"\b(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?)\b")        # e.g. 101,000.00
_AMT_PLAIN = re.compile(r"\b(\d{2,})(?!\d)\b")                           # plain digits

def parse_amount_any(line: str) -> Optional[float]:
    s = line.upper().replace("CR", "").replace("DR", "").replace(" ", "")
    s = _fix_ocr_zeros(s)
    # Indian style with last 2 as decimals: 1,01,000,00 => 101000.00
    m = _AMT_INR_DEC_LAST2.search(s)
    if m:
        raw = m.group(1).replace(",", "")
        if len(raw) >= 3:
            intp, dec = raw[:-2], raw[-2:]
            try:
                return float(f"{int(intp)}.{dec}")
            except Exception:
                pass
    # Western 101,000.00
    m = _AMT_WEST.search(s)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except Exception:
            pass
    # Plain big integer, assume no decimals
    m = _AMT_PLAIN.search(s)
    if m and len(m.group(1)) >= 3:
        try:
            return float(m.group(1))
        except Exception:
            pass
    return None

# ===== Director / master helpers (for authorised signatory selection) =====

def _load_master(company_name: str) -> dict:
    """
    Load latest _master_*.json for a company, same style as GST authorised signatory.
    """
    cdir = _company_folder(company_name)
    mp = _latest_master(cdir)
    if not mp:
        raise HTTPException(404, f"Master not found for company: {company_name}")
    _log(f"Using master for directors: {mp}")
    return read_json(mp)

# ===== API: packs & templates =====
@router.get("/packs", summary="List available pack keys")
def list_packs() -> List[PackKey]:
    return ["opc", "private_limited", "section8"]

@router.get("/templates", summary="List .docx templates for a pack")
def list_templates(pack: PackKey = Query(...)):
    pdir = _find_pack_dir(pack)
    files = _list_docx(pdir)
    return {
        "pack": pack,
        "folder": str(pdir),
        "files": [{"file": f.name, "path": str(f)} for f in files],
    }

# ===== API: MOA ingest =====
@router.post("/ingest/moa", summary="Locate & parse INC-33 MOA PDF for a company")
def ingest_moa(company_name: str = Body(..., embed=True)):
    p = find_moa_pdf(company_name)
    if not p:
        raise HTTPException(404, "MOA (INC-33) PDF not found in company folder")

    incdir = company_inc20a_dir(company_name)

    text = pdf_to_text(p)
    # Save raw for debugging
    raw_txt_file = incdir / f"moa_text_{_now_stamp()}.txt"
    raw_txt_file.write_text(text or "", encoding="utf-8")

    # Also read any form fields
    form_vals = pdf_form_values(p)

    if not (text.strip() or form_vals):
        out = incdir / f"moa_parsed_{_now_stamp()}.json"
        save_json(out, {"source": str(p), "parsed": {}, "warning": "No text/fields extracted from MOA"})
        return {"status": "ok", "file": str(p), "out_json": str(out), "parsed": {}, "warning": "Empty MOA extract."}

    parsed = parse_moa_text(text, form_vals)
    out = incdir / f"moa_parsed_{_now_stamp()}.json"
    save_json(out, {"source": str(p), "parsed": parsed})
    return {"status": "ok", "file": str(p), "out_json": str(out), "parsed": parsed}

# ===== API: Bank ingest (upload + OCR + parse) =====
@router.post("/ingest/bank", summary="Upload bank scan & OCR")
def ingest_bank(company_name: str = Form(...), file: UploadFile = File(...)):
    incdir = company_inc20a_dir(company_name)
    ext = (file.filename or "img").split(".")[-1]
    raw_path = incdir / "inputs" / "bank" / f"scan_{_now_stamp()}.{ext}"
    raw_path.write_bytes(file.file.read())

    text = ocr_image_to_text(raw_path)
    if not text.strip():
        raise HTTPException(
            500,
            "OCR failed. Install one of: (1) easyocr (pip install easyocr), OR "
            "(2) system Tesseract + pytesseract (install Tesseract and add to PATH)."
        )

    # save raw OCR text directly under INC20A
    (incdir / f"bank_ocr_{_now_stamp()}.txt").write_text(text, encoding="utf-8")

    parsed = parse_bank_text(text)
    out_json = incdir / f"bank_parsed_{_now_stamp()}.json"
    save_json(out_json, {"source": str(raw_path), "parsed": parsed})
    return {"status": "ok", "file": str(raw_path), "out_json": str(out_json), "parsed": parsed}

# ===== API: Autofill (merged latest MOA + Bank) =====
@router.get("/autofill", summary="Return merged latest MOA+Bank extracted data")
def autofill(company: str):
    data = get_autofill_data(company)
    if not data:
        raise HTTPException(404, "No extracted data found. Ingest MOA and/or Bank scan first.")
    return {"status": "ok", "company": company, "data": data}

# ===== API: Master + Directors (for INC-20A authorised signatory UI) =====

@router.get("/master", summary="Return raw master JSON for a company (INC-20A helper)")
def get_master(company_name: str = Query(...)):
    return _load_master(company_name)

@router.get("/directors", summary="List directors from master for INC-20A UI")
def get_directors(company_name: str = Query(...)):
    data = _load_master(company_name)
    directors = (
        data.get("directors")
        or (data.get("company") or {}).get("directors")
        or (data.get("company_master") or {}).get("directors")
        or []
    )
    return {
        "company_name": company_name,
        "directors": directors,
    }

# ===== Generate (with overrides & optional Bank Certificate) =====

class Signatory(BaseModel):
    name: str
    din: Optional[str] = None
    designation: Optional[str] = "Director"

class GenReq(BaseModel):
    company_name: str
    pack: PackKey
    selected_files: Optional[List[str]] = None
    date: Optional[str] = None
    use_autofill: Optional[bool] = False
    render_bank_certificate: Optional[bool] = True
    overrides: Optional[Dict[str, Any]] = None  # { "moa": {...}, "bank": {...}, "credits": [...] }
    signatory: Optional[Signatory] = None       # chosen authorised director for BR/Bank Certificate

@router.post("/generate", summary="Generate INC-20A pack for a company")
def generate(payload: GenReq):
    company = payload.company_name
    pdir = _find_pack_dir(payload.pack)
    all_files = _list_docx(pdir)
    if not all_files:
        raise HTTPException(404, f"No .docx templates in {pdir}")

    if payload.selected_files:
        keep = set(payload.selected_files)
        want = [p for p in all_files if p.name in keep]
        if not want:
            raise HTTPException(400, "None of the selected_files matched")
    else:
        want = all_files

    cfolder = _company_folder(company)
    master = _latest_master(cfolder)
    _log(f"Using master: {master}")

    # base context from your masters
    ctx = build_context(master, explicit_date=payload.date)
    if not isinstance(ctx, dict):
        ctx = dict(ctx or {})

    # Merge latest autofill (MOA+Bank) if requested
    if payload.use_autofill:
        af = get_autofill_data(company)
        if af.get("moa"):
            ctx.setdefault("moa", {})
            ctx["moa"] |= af["moa"]
        if af.get("bank"):
            ctx.setdefault("bank", {})
            ctx["bank"] |= af["bank"]
        if af.get("credits"):
            ctx["credits"] = af["credits"]

    # Apply explicit overrides from UI (final authority)
    if payload.overrides:
        for k, v in payload.overrides.items():
            if isinstance(v, dict):
                ctx.setdefault(k, {})
                ctx[k] |= v
            else:
                ctx[k] = v

    # Inject chosen signatory (authorised director) into context
    if payload.signatory:
        sig = payload.signatory.dict()
        ctx["signatory"] = sig             # {{ signatory.name }}, {{ signatory.din }}, {{ signatory.designation }}
        ctx.setdefault("extra", {})
        ctx["extra"]["signatory"] = sig    # also available under {{ extra.signatory.* }}

        # Ensure Jinja-safe defaults so templates never see undefined "bank"/"moa"/"credits"
    if not isinstance(ctx.get("bank"), dict):
        ctx["bank"] = {}
    if not isinstance(ctx.get("moa"), dict):
        ctx["moa"] = {}
    if not isinstance(ctx.get("credits"), list):
        ctx["credits"] = []

    items: List[Dict[str, Any]] = []

    # Render pack templates
    for tpl in want:
        tpl_id = f"inc20a__{_safe(payload.pack)}__{_safe(tpl.stem)}"
        out_path = render_docx(tpl, cfolder, ctx, tpl_id)
        items.append({
            "template": tpl.name,
            "output_docx": str(out_path),
            "download_url": _download_url(out_path),
        })
        _log(f"+ {out_path.name}")

    # ===== Bank Certificate: search in (1) current pack, (2) OPC folder, (3) common =====
    if payload.render_bank_certificate:
        current_pack_dir = pdir
        opc_dir = TEMPLATES_ROOT / "OPC"
        common_dir = TEMPLATES_ROOT / "common"

        candidates = [
            current_pack_dir / "Bank_Certificate_INC20A.docx",
            current_pack_dir / "Bank_Certificate_INC20A_formatonly.docx",
            opc_dir / "Bank_Certificate_INC20A.docx",
            opc_dir / "Bank_Certificate_INC20A_formatonly.docx",   # your provided path
            common_dir / "Bank_Certificate_INC20A.docx",
            common_dir / "Bank_Certificate_INC20A_formatonly.docx",
        ]
        bc_tpl = next((c for c in candidates if c.exists()), None)
        if bc_tpl:
            tpl_id = f"bank_certificate_inc20a_{_safe(company)}"
            out_path = render_docx(bc_tpl, cfolder, ctx, tpl_id)
            items.append({
                "template": bc_tpl.name,
                "output_docx": str(out_path),
                "download_url": _download_url(out_path),
            })
            _log(f"+ {out_path.name} (bank certificate)")
        else:
            _log("Bank certificate template not found in pack/OPC/common")

    return {
        "status": "SUCCESS",
        "company": company,
        "pack": payload.pack,
        "count": len(items),
        "items": items,
    }
