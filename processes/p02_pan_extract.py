#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
p02_pan_extract.py — PAN extractor

- Outputs saved relative to code:
    companies/<COMPANY>/outputs/pan_extract_<timestamp>.(json|txt)
- Master saved as BOTH:
    companies/<COMPANY>/_master_<slug>.json
    companies/<COMPANY>/_master_<slug>_<timestamp>.json
"""

# =========================
# SECTION 0: Imports
# =========================
import os, re, json, argparse, hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

# =========================
# SECTION 1: PDF & OCR libs (best-effort)
# =========================
PDF_EXTRACT_AVAILABLE = False
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
    PDF_EXTRACT_AVAILABLE = True
except Exception:
    pass

OCR_AVAILABLE = False
PDF2IMAGE_AVAILABLE = False
try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
    # Optional: set tesseract path if needed
    # pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
except Exception:
    pass

# =========================
# SECTION 2: Config & utils
# =========================
def log(msg: str): print(f"[pan_extract] {msg}")

def now_ts() -> str: return datetime.now().strftime("%Y-%m-%d %H:%M:%S%z")
def now_tag() -> str: return datetime.now().strftime("%Y%m%d_%H%M%S")

def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_dir(p: Path): p.mkdir(parents=True, exist_ok=True)

def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name).strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:80] or "company"

def path_company_hint(file_path: str) -> Optional[str]:
    parts = Path(file_path).resolve().parts
    for i, part in enumerate(parts):
        if part.lower() == "companies" and i + 1 < len(parts):
            return parts[i + 1]
    return None

def normalize_company_name_for_match(s: Optional[str]) -> str:
    if not s: return ""
    s = s.upper()
    s = re.sub(r"\bPRIVATE LIMITED\b|\bPVT\.?\s*LTD\b|\bPVT\b|\bLIMITED\b|\bLTD\b", "", s)
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s

def clean_company_name(n: Optional[str]) -> Optional[str]:
    if not n: return None
    s = n.strip()
    s = re.sub(r"^(?:AMA\s*/\s*)?NAME\s*[:\-]?\s*", "", s, flags=re.I)
    s = re.sub(r"^(?:नाम\s*/\s*)?Name\s*[:\-]?\s*", "", s, flags=re.I)
    s = re.sub(r"^\bName\b\s*[:\-]?\s*", "", s, flags=re.I)
    s = re.sub(r"[^A-Za-z0-9 &().'/\-]", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s.upper() if s else None

def safe_folderify(n: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]+', " ", n)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or "UNKNOWN COMPANY"

# Stable project root relative to code
BASE_DIR = Path(__file__).resolve().parents[1]     # parent of 'processes'
COMPANIES_ROOT = Path(os.getenv("COMPANIES_ROOT", str(BASE_DIR / "companies")))

# =========================
# SECTION 3: Load text (PDF/image)
# =========================
def extract_text_from_pdf(path: str) -> str:
    txt = ""
    if PDF_EXTRACT_AVAILABLE:
        log("Attempting PDF text extraction via pdfminer...")
        try:
            txt = pdfminer_extract_text(path) or ""
        except Exception as e:
            log(f"pdfminer failed: {e!r}")
    if txt.strip():
        log(f"PDF text extracted ({len(txt)} chars).")
        return txt

    log("PDF text empty/failed. Trying OCR fallback...")
    if PDF2IMAGE_AVAILABLE and OCR_AVAILABLE:
        try:
            pages = convert_from_path(path, dpi=300)
            buf = []
            for i, img in enumerate(pages, 1):
                log(f"OCR on page {i}...")
                buf.append(pytesseract.image_to_string(img))
            txt = "\n".join(buf)
            if txt.strip():
                log(f"OCR extracted ({len(txt)} chars).")
                return txt
        except Exception as e:
            log(f"OCR fallback failed: {e!r}")
    log("No text extracted.")
    return ""

def extract_text_from_image(path: str) -> str:
    if not OCR_AVAILABLE:
        log("OCR not available.")
        return ""
    try:
        log("Running OCR on image...")
        return pytesseract.image_to_string(Image.open(path))
    except Exception as e:
        log(f"OCR failed: {e!r}")
        return ""

def load_text_from_any(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf": return extract_text_from_pdf(path)
    if ext in (".png",".jpg",".jpeg",".tif",".tiff",".bmp",".webp"):
        return extract_text_from_image(path)
    return extract_text_from_pdf(path) or extract_text_from_image(path)

# =========================
# SECTION 4: Normalize text
# =========================
def normalize_text(raw: str) -> str:
    if not raw: return ""
    txt = raw.replace("\u2013","-").replace("\u2014","-").replace("\u00A0"," ")
    txt = re.sub(r"[ \t]+"," ", txt)
    txt = re.sub(r"\r\n?", "\n", txt)
    lines = [ln.strip() for ln in txt.split("\n")]
    while lines and not lines[0]: lines.pop(0)
    out = "\n".join(lines)
    log(f"Normalized text length: {len(out)}")
    return out

# =========================
# SECTION 5: PAN parsing (kept from your working version, with logs)
# =========================
PAN_REGEX = r"\b([A-Z]{5}\d{4}[A-Z])\b"

def pick_first(regex: str, text: str) -> Optional[str]:
    m = re.search(regex, text, flags=re.I)
    return (m.group(1) if m and m.groups() else (m.group(0) if m else None))

def get_value_after_label(lines: List[str], label_variants: List[str], lookahead: int = 8) -> Optional[str]:
    labset = [v.lower() for v in label_variants]
    for i, ln in enumerate(lines):
        l = ln.lower()
        if any(v == l or v in l for v in labset):
            candidates = []
            for j in range(1, lookahead + 1):
                if i + j >= len(lines): break
                cand = lines[i + j].strip()
                if not cand: continue
                low = cand.lower()
                if any(v in low for v in labset): continue
                if re.fullmatch(r"[A-Za-z &'()./-]{5,}", cand):
                    candidates.append(cand)
            if candidates:
                best = max(candidates, key=len)
                log(f"[label-scan] candidates={candidates} → best='{best}'")
                return best
    return None

def guess_company_name_from_text(lines: List[str], pan_number: Optional[str]) -> Optional[str]:
    blacklist = {
        "INCOME TAX DEPARTMENT",
        "GOVT. OF INDIA",
        "E - PERMANENT ACCOUNT NUMBER (E-PAN) CARD",
        "ई - स्थायी लेखा संख्या कार्ड"
    }
    candidates = []
    for ln in lines:
        t = ln.strip()
        if not t: continue
        if pan_number and pan_number in t: continue
        T = t.upper()
        if T in blacklist: continue
        if re.fullmatch(r"[A-Z0-9 &().'/\-]{8,}", T):
            candidates.append(T)
    if not candidates:
        return None
    choice = max(candidates, key=len)
    log(f"[name-fallback] candidates={len(candidates)}  chosen='{choice}'")
    return choice

def parse_pan_blocks(text: str) -> Dict[str, Any]:
    log("Parsing PAN fields…")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    joined = "\n".join(lines)

    pan = pick_first(PAN_REGEX, joined)
    name_from_label = get_value_after_label(
        lines, ["नाम / Name", "Name", "Name on Card", "Name of Applicant", "Name as on PAN card"], lookahead=5
    )
    name_fallback   = guess_company_name_from_text(lines, pan)

    def clean_company_name(n: Optional[str]) -> Optional[str]:
        if not n: return None
        s = n.strip()
        s = re.sub(r"^(?:AMA\s*/\s*)?NAME\s*[:\-]?\s*", "", s, flags=re.I)
        s = re.sub(r"^(?:नाम\s*/\s*)?Name\s*[:\-]?\s*", "", s, flags=re.I)
        s = re.sub(r"^\bName\b\s*[:\-]?\s*", "", s, flags=re.I)
        s = re.sub(r"[^A-Za-z0-9 &().'/\-]", " ", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s.upper() if s else None

    holder_name_raw = name_from_label if name_from_label and len(name_from_label) >= 5 else name_fallback
    holder_name = clean_company_name(holder_name_raw)

    doi = (
        pick_first(r"Date\s*of\s*Incorporation\s*/\s*Formation[^0-9]*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})", joined) or
        get_value_after_label(lines, ["Date of Incorporation / Formation", "Date of Incorporation"], lookahead=5) or
        pick_first(r"Date\s*of\s*Incorporation\s*[:\-]?\s*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})", joined)
    )
    dob = pick_first(r"Date\s*of\s*Birth\s*[:\-]?\s*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})", joined)

    mcat = pick_first(r"Category\s*[:\-]?\s*(Individual|Company|Firm|HUF|AOP|Trust|LLP|Local Authority|Government)", joined)
    cat = mcat or ("Company" if doi and not dob else None)

    log(f"PAN: {pan}")
    log(f"Name (label): {name_from_label}")
    log(f"Name (fallback): {name_fallback}")
    log(f"Name (chosen): {holder_name}")
    log(f"DOI: {doi}  DOB: {dob}  Category: {cat}")

    return {
        "pan_number": pan,
        "pan_name": holder_name,
        "date_of_incorporation": doi,
        "date_of_birth": dob,
        "category_guess": cat or ("Company" if holder_name and " PRIVATE " in holder_name else None)
    }

# =========================
# SECTION 6: Master updater (create/update)
# =========================
def load_or_create_master(company_name: str, folder_company: Path) -> Dict[str, Any]:
    stable = folder_company / f"_master_{slugify(company_name)}.json"
    if stable.exists():
        try:
            data = json.loads(stable.read_text(encoding="utf-8"))
            log(f"Loaded existing master: {stable}")
            return data
        except Exception as e:
            log(f"Failed to read master; creating new. Error: {e!r}")
    log("Creating new master JSON.")
    return {
        "company_name": company_name,
        "cin": None,
        "pan": None,
        "registered_address": None,
        "docs": [],
        "mismatches": {},
        "process_log": [],
        "meta": {"created": now_ts(), "last_updated": now_ts(), "version": 1}
    }

def save_master(company_name: str, folder_company: Path, master: Dict[str, Any]) -> str:
    slug = slugify(company_name)
    ts   = now_tag()
    # Save both a stable and a timestamped master (so API glob "_master_*.json" finds it)
    stable = folder_company / f"_master_{slug}.json"
    dated  = folder_company / f"_master_{slug}_{ts}.json"
    master["meta"]["last_updated"] = now_ts()
    payload = json.dumps(master, indent=2, ensure_ascii=False)
    stable.write_text(payload, encoding="utf-8")
    dated.write_text(payload, encoding="utf-8")
    log(f"Master JSON saved: {stable}")
    log(f"Master JSON (timestamped) saved: {dated}")
    return str(dated)

def update_master_with_pan(master: Dict[str, Any], pan_block: Dict[str, Any], file_hash: str, src_file: str):
    master["pan"] = {
        "pan_number": pan_block.get("pan_number"),
        "pan_name": pan_block.get("pan_name"),
        "category": pan_block.get("category_guess"),
        "dob_or_doi": pan_block.get("date_of_birth") or pan_block.get("date_of_incorporation"),
        "source": "PAN_IMPORT"
    }
    doc = {
        "doc_type": "PAN",
        "source": "upload",
        "file": src_file,
        "hash": file_hash,
        "extracted": pan_block
    }
    master.setdefault("docs", []).append(doc)
    master.setdefault("process_log", []).append({
        "process": "PAN_IMPORT",
        "ts": now_ts(),
        "inputs": {"file": src_file, "file_hash": file_hash},
        "outputs": {"pan_number": pan_block.get("pan_number"), "pan_name": pan_block.get("pan_name")},
        "status": "SUCCESS"
    })

# =========================
# SECTION 7: Writer (outputs + master)
# =========================
def write_outputs(parsed: Dict[str, Any], clean_text: str, company_name: str) -> Dict[str, str]:
    companies_root = COMPANIES_ROOT
    folder_company = companies_root / safe_folderify(company_name)
    folder_outputs = folder_company / "outputs"

    log(f"[paths] BASE_DIR={BASE_DIR}")
    log(f"[paths] COMPANIES_ROOT={companies_root}")
    log(f"[paths] Company folder={folder_company}")

    ensure_dir(folder_company)
    ensure_dir(folder_outputs)

    tag = now_tag()
    json_path = folder_outputs / f"pan_extract_{tag}.json"
    txt_path  = folder_outputs / f"pan_extract_{tag}.txt"

    log(f"Writing text to: {txt_path}")
    txt_path.write_text(clean_text, encoding="utf-8")
    log(f"Writing JSON to: {json_path}")
    json_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"json": str(json_path), "text": str(txt_path)}

# =========================
# SECTION 8: Orchestrator
# =========================
def run(path: str) -> Dict[str, Any]:
    path = str(Path(path).resolve())
    log(f"Started PAN extraction for: {path}")
    log(f"[paths] BASE_DIR={BASE_DIR}")
    log(f"[paths] COMPANIES_ROOT={COMPANIES_ROOT}")

    file_hash = sha256_of_file(path)
    log(f"File SHA-256: {file_hash}")

    raw = load_text_from_any(path)
    clean = normalize_text(raw)
    if not clean.strip():
        log("No text could be extracted. Exiting.")
        return {"status":"FAILED","message":"No text extracted","file":path,"file_hash":file_hash}

    block = parse_pan_blocks(clean)

    hint = path_company_hint(path)
    company_name = hint or (block.get("pan_name") or "UNKNOWN COMPANY")
    company_name = clean_company_name(company_name) or "UNKNOWN COMPANY"

    parsed = {
        "source_file": path,
        "file_hash_sha256": file_hash,
        "parse_meta": {"parser":"p02_pan_extract.py","version":"1.3.1","timestamp": now_ts()},
        "company_name": company_name,
        "pan_data": block,
        "raw_text_first_8k": clean[:8000]
    }
    outs = write_outputs(parsed, clean, company_name)

    folder_company = COMPANIES_ROOT / safe_folderify(company_name)
    ensure_dir(folder_company)
    master = load_or_create_master(company_name, folder_company)
    update_master_with_pan(master, block, file_hash, path)
    master_path = save_master(company_name, folder_company, master)

    log("PAN extraction complete.")
    log(f"JSON: {outs['json']}")
    log(f"Text: {outs['text']}")
    log(f"Master: {master_path}")

    return {
        "status": "SUCCESS",
        "message": "PAN extraction complete",
        "outputs": {"json": outs["json"], "text": outs["text"], "master": master_path},
        "company_name": company_name,
        "pan_number": block.get("pan_number")
    }

# =========================
# SECTION 9: CLI
# =========================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Extract PAN fields from PDF/image and update master JSON.")
    ap.add_argument("--file", required=True, help="Path to PAN file")
    args = ap.parse_args()
    res = run(args.file)
    print("\n=== RESULT ===")
    print(json.dumps(res, indent=2, ensure_ascii=False))
