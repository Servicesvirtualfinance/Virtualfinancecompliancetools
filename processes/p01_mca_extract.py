#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
p01_mca_extract.py — MCA extractor (robust)

- Input: path to an MCA Master-Data PDF (or image)
- Outputs (relative to project root):
    companies/<COMPANY>/outputs/
      ├─ mca_extract_<YYYYMMDD_HHMMSS>.json
      └─ mca_extract_<YYYYMMDD_HHMMSS>.txt

Key robustness upgrades:
- pdfminer "advanced" extraction with LAParams + extract_text_to_fp
- Inline "Header Value" parsing (e.g., "CIN U63910...")
- Removes common MCA headers/footers/time-stamps from extracted text
- Normalizes obfuscated emails like [dot]/[at]
- Stronger director table parsing when DIN appears on same line as Sr. No
"""

# =========================
# SECTION 0: Imports
# =========================
import os
import re
import json
import argparse
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

# =========================
# SECTION 1: PDF & OCR libs (best-effort)
# =========================
PDF_EXTRACT_AVAILABLE = False
PDFMINER_ADV_AVAILABLE = False
try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
    from pdfminer.high_level import extract_text_to_fp
    from pdfminer.layout import LAParams
    from io import StringIO
    PDF_EXTRACT_AVAILABLE = True
    PDFMINER_ADV_AVAILABLE = True
except Exception:
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract_text
        PDF_EXTRACT_AVAILABLE = True
    except Exception:
        PDF_EXTRACT_AVAILABLE = False

OCR_AVAILABLE = False
PDF2IMAGE_AVAILABLE = False
try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except Exception:
    pass

PYMUPDF_AVAILABLE = False
PYPDF_AVAILABLE = False
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except Exception:
    pass
try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except Exception:
    pass

# pdfplumber (coordinate-accurate address extraction)
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except Exception:
    PDFPLUMBER_AVAILABLE = False

# Windows helpers (optional): set paths if not on PATH
POPPLER_PATH = os.getenv("POPPLER_PATH", "")   # e.g. r"C:\poppler-24.08.0\Library\bin"
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "") # e.g. r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if TESSERACT_CMD and OCR_AVAILABLE:
    try:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    except Exception:
        pass

TESSERACT_LANG = os.getenv("TESSERACT_LANG", "eng")
TESSERACT_CONFIG = os.getenv("TESSERACT_CONFIG", "--psm 6")

# =========================
# SECTION 2: Config & small utils
# =========================
THIS_FILE = Path(__file__).resolve()
BASE_DIR = THIS_FILE.parents[1] if len(THIS_FILE.parents) >= 2 else THIS_FILE.parent
COMPANIES_ROOT = Path(os.getenv("COMPANIES_ROOT", str(BASE_DIR / "companies")))

def log(msg: str) -> None:
    print(f"[mca_extract] {msg}")

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S%z")

def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def path_company_hint(file_path: str) -> Optional[str]:
    """If file is already under companies/<COMPANY>/..., return that company name."""
    p = Path(file_path).resolve().parts
    for i, part in enumerate(p):
        if part.lower() == "companies" and i + 1 < len(p):
            return p[i + 1]
    return None

def safe_folderify(n: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]+', " ", (n or ""))
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or "UNKNOWN COMPANY"

# =========================
# SECTION 3: Load text
# =========================

def extract_text_with_pdfminer_advanced(path: str) -> str:
    if not (PDF_EXTRACT_AVAILABLE and PDFMINER_ADV_AVAILABLE):
        return ""
    try:
        log("Attempting PDF text extraction via pdfminer (advanced LAParams)...")
        output = StringIO()
        laparams = LAParams(
            line_margin=0.15,
            char_margin=1.5,
            word_margin=0.10,
            boxes_flow=None
        )
        with open(path, "rb") as f:
            extract_text_to_fp(f, output, laparams=laparams)
        txt = output.getvalue() or ""
        return txt
    except Exception as e:
        log(f"pdfminer advanced failed: {e!r}")
        return ""

def extract_text_with_pdfminer_simple(path: str) -> str:
    if not PDF_EXTRACT_AVAILABLE:
        return ""
    try:
        log("Attempting PDF text extraction via pdfminer (simple)...")
        return pdfminer_extract_text(path) or ""
    except Exception as e:
        log(f"pdfminer simple failed: {e!r}")
        return ""

def extract_text_with_pymupdf(path: str) -> str:
    if not PYMUPDF_AVAILABLE:
        return ""
    try:
        txt_parts = []
        with fitz.open(path) as doc:
            for page in doc:
                txt_parts.append(page.get_text("text") or "")
        return "\n".join(txt_parts)
    except Exception as e:
        log(f"PyMuPDF failed: {e!r}")
        return ""

def extract_text_with_pypdf(path: str) -> str:
    if not PYPDF_AVAILABLE:
        return ""
    try:
        reader = PdfReader(path)
        parts = []
        for p in reader.pages:
            try:
                parts.append(p.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    except Exception as e:
        log(f"pypdf failed: {e!r}")
        return ""

def ocr_pdf(path: str) -> str:
    if not (PDF2IMAGE_AVAILABLE and OCR_AVAILABLE):
        return ""
    try:
        kwargs = {"dpi": 300}
        if POPPLER_PATH:
            kwargs["poppler_path"] = POPPLER_PATH
        pages = convert_from_path(path, **kwargs)
        ocr_parts = []
        for i, img in enumerate(pages, 1):
            log(f"OCR on page {i}...")
            ocr_parts.append(pytesseract.image_to_string(img, lang=TESSERACT_LANG, config=TESSERACT_CONFIG) or "")
        return "\n".join(ocr_parts)
    except Exception as e:
        log(f"OCR fallback failed: {e!r}")
        return ""

def extract_text_from_pdf(path: str) -> str:
    # 1) pdfminer advanced
    txt = extract_text_with_pdfminer_advanced(path)
    if txt.strip():
        log(f"pdfminer(advanced) extracted ({len(txt)} chars).")
        return txt

    # 2) pdfminer simple
    txt = extract_text_with_pdfminer_simple(path)
    if txt.strip():
        log(f"pdfminer(simple) extracted ({len(txt)} chars).")
        return txt

    # 3) PyMuPDF
    txt = extract_text_with_pymupdf(path)
    if txt.strip():
        log(f"PyMuPDF extracted ({len(txt)} chars).")
        return txt

    # 4) pypdf
    txt = extract_text_with_pypdf(path)
    if txt.strip():
        log(f"pypdf extracted ({len(txt)} chars).")
        return txt

    # 5) OCR
    log("PDF text empty/failed. Trying OCR fallback...")
    txt = ocr_pdf(path)
    if txt.strip():
        log(f"OCR extracted ({len(txt)} chars).")
        return txt

    log("No text extracted.")
    return ""

def extract_text_from_image(path: str) -> str:
    if not OCR_AVAILABLE:
        log("OCR not available.")
        return ""
    try:
        log("Running OCR on image...")
        img = Image.open(path)
        return pytesseract.image_to_string(img, lang=TESSERACT_LANG, config=TESSERACT_CONFIG) or ""
    except Exception as e:
        log(f"OCR failed: {e!r}")
        return ""

def load_text_from_any(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(path)
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"):
        return extract_text_from_image(path)
    # unknown: try both
    return extract_text_from_pdf(path) or extract_text_from_image(path)

# =========================
# SECTION 4: Clean / Normalize
# =========================
MCA_HEADER_FOOTER_RX = [
    re.compile(r"^\s*Ministry\s+Of\s+Corporate\s+Affairs\s*$", re.I),
    re.compile(r"^\s*Ministry\s+of\s+Corporate\s+Affairs\s*-\s*MCA\s+Services\s*$", re.I),
    re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*.*MCA\s+Services\s*$", re.I),
    re.compile(r"^\s*Date\s*:\s*\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s+.*$", re.I),
]

def remove_mca_headers_footers(raw: str) -> str:
    if not raw:
        return ""
    out_lines = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            out_lines.append("")
            continue
        if any(rx.match(s) for rx in MCA_HEADER_FOOTER_RX):
            continue
        out_lines.append(ln)
    return "\n".join(out_lines)

def normalize_text(raw: str) -> str:
    if not raw:
        return ""
    txt = remove_mca_headers_footers(raw)

    # normalize dashes & NBSP
    txt = txt.replace("\u2013", "-").replace("\u2014", "-").replace("\u00A0", " ")

    # normalize obfuscated emails: [dot]/[at], (dot)/(at)
    txt = re.sub(r"\[dot\]|\(dot\)", ".", txt, flags=re.I)
    txt = re.sub(r"\[at\]|\(at\)", "@", txt, flags=re.I)

    # tighten whitespace
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\r\n?", "\n", txt)

    # trim per-line
    lines = [ln.strip() for ln in txt.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    out = "\n".join(lines)
    log(f"Normalized text length: {len(out)}")
    return out

# =========================
# SECTION 5: Parsing helpers
# =========================
KNOWN_HEADERS = [
    "company information", "cin", "company name", "roc name", "registration number",
    "date of incorporation", "email id", "registered address",
    "address at which the books of account", "listed in stock exchange(s) (y/n)",
    "category of company", "subcategory of the company", "class of company",
    "active compliance", "authorised capital (rs)", "paid up capital (rs)",
    "date of last agm", "date of balance sheet", "company status",
    "jurisdiction", "roc (name and office)", "rd (name and region)",
    "index of charges", "director/signatory details", "quick links", "disclaimer",
    "home", "master data", "sitemap", "theme light", "language english", "search", "follow us",
    "latest news", "principal accounts office", "help & faqs", "about us", "xbrl v3",
    "small company"
]

def is_header_line(line: str) -> bool:
    l = line.strip().lower()
    if not l:
        return False
    if len(l) <= 3:
        return True
    return l in KNOWN_HEADERS or any(l.startswith(h) for h in KNOWN_HEADERS)

def get_value_after_header(text: str, header_regex: str, max_lookahead: int = 15) -> Optional[str]:
    """
    Works for both:
      - header on its own line, value on next lines
      - inline 'Header Value' on same line, e.g., 'CIN U63910GJ...'
    """
    pat = re.compile(header_regex, re.I)
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        ln_s = ln.strip()
        if not ln_s:
            continue

        m = pat.search(ln_s)
        if not m:
            continue

        # 1) Inline remainder after matched header
        remainder = ln_s[m.end():].strip()
        remainder = re.sub(r"^[\s:–—-]+", "", remainder).strip()
        if remainder:
            return remainder

        # 2) Next non-header line(s)
        for j in range(1, max_lookahead + 1):
            if i + j >= len(lines):
                break
            cand = lines[i + j].strip()
            if not cand:
                continue
            if is_header_line(cand):
                continue
            return cand
    return None

def get_block_after_header(
    text: str,
    header_regex: str,
    stop_headers: List[str],
    max_lines: int = 40
) -> Optional[str]:
    pat = re.compile(header_regex, re.I)
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if not pat.search(ln.strip()):
            continue
        bucket, blanks = [], 0
        for j in range(i + 1, min(i + 1 + max_lines, len(lines))):
            cand = lines[j].strip()
            if not cand:
                blanks += 1
                if blanks <= 1:
                    continue
                if bucket:
                    break
                continue
            blanks = 0
            if any(re.fullmatch(h, cand, re.I) or cand.lower().startswith(h.lower()) for h in stop_headers):
                break
            if is_header_line(cand) and bucket:
                break
            bucket.append(cand)
        out = " ".join(bucket).strip()
        out = re.sub(r"\s+,", ",", out)
        return out or None
    return None

def is_indian_amount(s: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d{4,}", (s or "").strip()))

def is_date_like(s: str) -> bool:
    # Accept 23/12/2025, 12/24/25, 23-12-2025, etc.
    return bool(re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", (s or "").strip())) or (s or "").strip() == "-"

def is_class_value(s: str) -> bool:
    s = (s or "").strip()
    allowed = {
        "Private", "Public", "One Person Company", "OPC", "Government Company",
        "Section 8 Company", "Not For Profit", "Producer Company", "Nidhi Company",
        "LLP", "Limited by Guarantee", "Unlimited"
    }
    return s.title() in allowed or s.upper() in {"PRIVATE", "PUBLIC"}

def parse_vertical_values_block_smart(text: str, headers: List[str]) -> Dict[str, str]:
    """
    Works best when headers appear as a vertical block and values are elsewhere.
    If not found, returns {} and caller should fallback to get_value_after_header.
    """
    lines = [ln.strip() for ln in text.split("\n")]
    idxs, pos = [], 0
    for h in headers:
        pat = re.compile(rf"^{re.escape(h)}$", re.I)
        found = None
        for i in range(pos, len(lines)):
            if pat.match(lines[i]):
                found = i
                break
        if found is None:
            return {}
        idxs.append(found)
        pos = found + 1

    last_idx = idxs[-1]
    cand = []
    for ln in lines[last_idx + 1:]:
        if not ln:
            continue
        if is_header_line(ln):
            continue
        cand.append(ln)

    out: Dict[str, str] = {}
    # heuristics
    for i, v in enumerate(list(cand)):
        if is_class_value(v):
            out["Class of Company"] = v
            cand.pop(i)
            break
    for i, v in enumerate(list(cand)):
        if v in ("Yes", "No", "-"):
            out["ACTIVE compliance"] = v
            cand.pop(i)
            break
    for i, v in enumerate(list(cand)):
        if is_indian_amount(v):
            out["Authorised Capital (Rs)"] = v
            cand.pop(i)
            break
    for i, v in enumerate(list(cand)):
        if is_indian_amount(v):
            out["Paid up Capital (Rs)"] = v
            cand.pop(i)
            break
    for i, v in enumerate(list(cand)):
        if is_date_like(v):
            out["Date of last AGM"] = v
            cand.pop(i)
            break
    for i, v in enumerate(list(cand)):
        if is_date_like(v):
            out["Date of Balance Sheet"] = v
            cand.pop(i)
            break
    return out

# =========================
# SECTION 5B: Email extraction (robust)
# =========================
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.I)

EMAIL_HEADER_PATTERNS = [
    r"Email\s*Id", r"Email\s*ID", r"E[-\s]?mail\s*Id", r"E[-\s]?mail\s*ID",
    r"Registered\s+Email\s*Id", r"Company\s*Email\s*Id", r"Email"
]

def _is_email(s: Optional[str]) -> bool:
    return bool(s) and bool(EMAIL_RX.fullmatch(s.strip()))

def get_value_after_header_flex(
    text: str,
    header_patterns: List[str],
    validator=None,
    max_lookahead: int = 12
) -> Optional[str]:
    lines = [ln.strip() for ln in text.split("\n")]
    pats = [re.compile(p + r"\s*:?\s*", re.I) for p in header_patterns]
    for i, ln in enumerate(lines):
        for pat in pats:
            m = pat.search(ln)
            if not m:
                continue

            # inline after the header:
            inline = ln[m.end():].strip()
            inline = re.sub(r"^[\s:–—-]+", "", inline).strip()
            if inline:
                cand = inline.split()[0] if validator == _is_email else inline
                if not validator or validator(cand):
                    return cand

            # next lines:
            for j in range(1, max_lookahead + 1):
                if i + j >= len(lines):
                    break
                nxt = lines[i + j].strip()
                if not nxt:
                    continue
                if is_header_line(nxt):
                    break
                cand = nxt.split()[0] if validator == _is_email else nxt
                if not validator or validator(cand):
                    return cand
    return None

def extract_email_field(text: str, company_name: Optional[str] = None) -> Optional[str]:
    v = get_value_after_header_flex(text, EMAIL_HEADER_PATTERNS, validator=_is_email)
    if v:
        return v

    found = sorted(set(EMAIL_RX.findall(text)))
    if not found:
        return None

    if not company_name:
        return found[0]

    toks = [t.lower() for t in re.split(r"[^a-zA-Z0-9]+", company_name) if len(t) >= 3]
    for e in found:
        dom = e.split("@")[-1].lower()
        if any(tok in dom for tok in toks):
            return e
    return found[0]

# =========================
# SECTION 5C: Registered address (pdfplumber bbox extractor)
# =========================
PIN_RX = re.compile(r"\b\d{6}\b")
BARE_PIN_RX = re.compile(r"^(?:india[, ]*)?\s*\d{6}\s*$", re.I)

STOP_HEADERS = (
    "Address at which the books of account",
    "Listed in Stock Exchange", "Listed in Stock Exchange(s)",
    "Last AGM Date", "Date of last AGM",
    "Balance Sheet Date", "Date of Balance Sheet",
    "Company Status", "Category of Company", "Subcategory of the Company",
    "Sub Category", "Class of Company", "ACTIVE compliance",
    "Authorised Capital", "Authorised Capital (Rs)",
    "Paid up Capital", "Paid up Capital (Rs)",
    "Jurisdiction", "ROC (name and office)", "RD (name and Region)",
    "Quick Links", "Email Id", "Email", "Small Company"
)

def _norm_addr(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", (s or "")).strip()
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"(,\s*){2,}", ", ", s)
    return s.strip(" ,;-")

def _is_stop_header_text(txt: str) -> bool:
    t = (txt or "").strip().lower()
    return any(t.startswith(h.lower()) for h in STOP_HEADERS if t)

def _group_words_into_lines(words, y_tol=2.2):
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines, cur, cur_top = [], [], None
    for w in words:
        if cur_top is None or abs(w["top"] - cur_top) <= y_tol:
            cur_top = w["top"] if cur_top is None else cur_top
            cur.append(w)
        else:
            lines.append((cur_top, cur))
            cur_top, cur = w["top"], [w]
    if cur:
        lines.append((cur_top, cur))

    out = []
    for top, ws in lines:
        ws = sorted(ws, key=lambda t: t["x0"])
        text = " ".join((w.get("text") or "") for w in ws if w.get("text"))
        out.append({"top": top, "words": ws, "text": text})
    return out

def _find_registered_address_label(lines):
    for ln in lines:
        ws = ln["words"]
        for i, w in enumerate(ws):
            if (w.get("text") or "").strip().lower() != "registered":
                continue
            band_top, band_bot = w["top"] - 3, w["top"] + 3
            for j in range(i + 1, min(i + 20, len(ws))):
                w2 = ws[j]
                if not (band_top <= w2["top"] <= band_bot):
                    continue
                if w2["x0"] <= w["x1"]:
                    continue
                if (w2.get("text") or "").strip().lower().startswith("address"):
                    bbox = {
                        "x0": min(w["x0"], w2["x0"]),
                        "x1": max(w["x1"], w2["x1"]),
                        "top": min(w["top"], w2["top"]),
                        "bottom": max(w["top"], w2["top"]) + max(w["height"], w2["height"]),
                    }
                    return ln, bbox
    return None, None

def _assemble_address_from_words(rect_words) -> str:
    addr_lines = _group_words_into_lines(rect_words)
    if not addr_lines:
        return ""

    kept = []
    for ln in addr_lines:
        t = _norm_addr(ln["text"])
        if not t or _is_stop_header_text(t):
            continue
        kept.append({"top": ln["top"], "text": t})

    if not kept:
        return ""

    # If a line with PIN exists and it's not last, move it to the end
    pin_idx = next((i for i, L in enumerate(kept) if PIN_RX.search(L["text"])), None)
    if pin_idx is not None and pin_idx != len(kept) - 1:
        kept.append(kept.pop(pin_idx))

    addr = _norm_addr(", ".join(L["text"] for L in kept))
    if not addr or len(addr) < 20 or BARE_PIN_RX.match(addr):
        return ""
    return addr

def extract_registered_address_bbox(pdf_path: str) -> str:
    """
    Coordinate-accurate extraction of 'Registered Address' with 2 passes:
      A) Right-of-label column
      B) Under-label block
    We UNION A and B, de-duplicate by (top,x0,text), regroup by Y, and assemble.
    """
    if not PDFPLUMBER_AVAILABLE or not pdf_path:
        return ""
    try:
        import statistics
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words(use_text_flow=True, keep_blank_chars=False) or []
                if not words:
                    continue
                lines = _group_words_into_lines(words)
                label_line, label_bbox = _find_registered_address_label(lines)
                if not label_line:
                    continue

                # infer column_x0 (right of label), allow slight left wiggle; avoid left column bleed
                right_x0s = [w["x0"] for w in label_line["words"] if w["x0"] > label_bbox["x1"] + 2]
                if right_x0s:
                    col_x0 = min(right_x0s)
                else:
                    probe_x0 = [w["x0"] for w in words if w["top"] > label_bbox["bottom"] + 1]
                    col_x0 = statistics.median(probe_x0) if probe_x0 else (label_bbox["x1"] + 6)

                col_x0 = max(col_x0 - 6, label_bbox["x1"] + 2)      # capture first line
                col_x0 = max(col_x0, page.width * 0.30)             # resist left column bleed

                # bottom boundary = first stop header below; else a generous slice
                bottom_y = None
                for ln in lines:
                    if ln["top"] <= label_bbox["bottom"] + 1:
                        continue
                    if _is_stop_header_text(ln["text"]):
                        bottom_y = ln["top"]
                        break
                if bottom_y is None:
                    bottom_y = min(page.height - 4, label_bbox["bottom"] + 320)

                # PASS A: right-of-label column
                rect_words_A = [
                    w for w in words
                    if (label_bbox["bottom"] - 1) <= w["top"] <= (bottom_y - 1)
                    and (w["x0"] >= col_x0)
                ]
                # PASS B: under-label block
                rect_words_B = [
                    w for w in words
                    if (label_bbox["bottom"] + 1) <= w["top"] <= (bottom_y - 1)
                    and (label_bbox["x0"] - 2) <= w["x0"] <= (page.width - 10)
                ]

                # UNION A ∪ B, de-dup by (rounded top, x0, text)
                def _key(w):
                    return (round(w["top"], 1), round(w["x0"], 1), w.get("text", ""))

                seen, union_words = set(), []
                for src in (rect_words_A, rect_words_B):
                    for w in src:
                        k = _key(w)
                        if k not in seen:
                            seen.add(k)
                            union_words.append(w)

                addr = _assemble_address_from_words(union_words)
                if addr:
                    return addr
    except Exception:
        return ""
    return ""

def reorder_address_lines_human(address_text: str) -> str:
    """
    Simple and safe: join pieces into one clean comma-separated address.
    Useful when text fallback returns newlines/commas inconsistently.
    """
    if not address_text:
        return ""
    parts = [p.strip().strip(",") for p in re.split(r"[\n,]+", address_text) if p and p.strip()]
    joined = ", ".join(parts)
    joined = re.sub(r"\s{2,}", " ", joined)
    joined = re.sub(r",\s*,+", ", ", joined)
    return _norm_addr(joined)

# =========================
# SECTION 5D: Master-data parsing
# =========================
def parse_master_data(text: str, pdf_path: Optional[str] = None) -> Dict[str, Any]:
    log("Parsing master data…")
    d: Dict[str, Any] = {}

    d["cin"] = get_value_after_header(text, r"\bCIN\b")
    d["company_name"] = get_value_after_header(text, r"\bCompany Name\b")
    d["roc_name"] = get_value_after_header(text, r"\bROC Name\b") or get_value_after_header(text, r"ROC \(name and office\)")
    d["registration_number"] = get_value_after_header(text, r"\bRegistration Number\b")
    d["date_of_incorporation"] = get_value_after_header(text, r"\bDate of Incorporation\b")
    d["email"] = extract_email_field(text, d.get("company_name"))

    # Registered Address — prefer coordinate extraction; fallback to text block
    addr_bbox = extract_registered_address_bbox(pdf_path) if pdf_path else ""
    if addr_bbox:
        d["registered_address"] = addr_bbox
    else:
        raw_block = get_block_after_header(
            text,
            r"Registered Address",
            stop_headers=[
                r"Address at which the books of account",
                r"Listed in Stock Exchange",
                r"Category of Company",
                r"Subcategory of the Company",
                r"Class of Company",
                r"ACTIVE compliance",
                r"Authorised Capital \(Rs\)",
                r"Paid up Capital \(Rs\)",
                r"Date of last AGM",
                r"Date of Balance Sheet",
                r"Company Status",
                r"Small Company",
                r"Jurisdiction",
                r"Director/Signatory details",
                r"ROC \(name and office\)",
                r"RD \(name and Region\)"
            ],
            max_lines=60
        )
        d["registered_address"] = reorder_address_lines_human(raw_block or "")

    # These may be inline in many MCA PDFs; keep vertical smart block as helper
    vheaders = [
        "Class of Company", "ACTIVE compliance",
        "Authorised Capital (Rs)", "Paid up Capital (Rs)",
        "Date of last AGM", "Date of Balance Sheet",
    ]
    vmap = parse_vertical_values_block_smart(text, vheaders)

    klass = vmap.get("Class of Company") or get_value_after_header(text, r"Class of Company")
    active = vmap.get("ACTIVE compliance") or get_value_after_header(text, r"ACTIVE compliance")
    auth = vmap.get("Authorised Capital (Rs)") or get_value_after_header(text, r"Authorised Capital \(Rs\)")
    paid = vmap.get("Paid up Capital (Rs)") or get_value_after_header(text, r"Paid up Capital \(Rs\)")
    agm = vmap.get("Date of last AGM") or get_value_after_header(text, r"Date of last AGM")
    bs = vmap.get("Date of Balance Sheet") or get_value_after_header(text, r"Date of Balance Sheet")

    def to_int_indian(x: Optional[str]) -> Optional[int]:
        if not x:
            return None
        x = x.strip()
        if x == "-":
            return None
        cleaned = re.sub(r"[^\d]", "", x)
        if not cleaned:
            return None
        try:
            return int(cleaned)
        except Exception:
            return None

    d["category_of_company"] = get_value_after_header(text, r"Category of Company")
    d["subcategory_of_company"] = get_value_after_header(text, r"Subcategory of the Company")
    d["class_of_company"] = None if (klass and is_header_line(klass)) else klass
    d["active_compliance"] = None if (active and is_header_line(active)) else (None if active == "-" else active)
    d["authorised_capital"] = to_int_indian(auth)
    d["paid_up_capital"] = to_int_indian(paid)
    d["date_of_last_agm"] = None if (agm == "-" or (agm and is_header_line(agm))) else agm
    d["date_of_balance_sheet"] = None if (bs == "-" or (bs and is_header_line(bs))) else bs

    d["company_status"] = get_value_after_header(text, r"Company Status")
    d["small_company"] = get_value_after_header(text, r"Small Company")

    d["jurisdiction_roc"] = get_value_after_header(text, r"ROC \(name and office\)")
    d["jurisdiction_rd"] = get_value_after_header(text, r"RD \(name and Region\)")

    emails = set(EMAIL_RX.findall(text))
    phones = set(re.findall(r"\b[6-9]\d{9}\b", text))
    d["emails_found"] = sorted(emails)
    d["phones_found"] = sorted(phones)

    log(f"CIN: {d.get('cin')}")
    log(f"Company Name: {d.get('company_name')}")
    log(f"Email: {d.get('email')}")
    log(f"Registered Address: {d.get('registered_address')}")
    return d

# =========================
# SECTION 6: Directors parsing (robust)
# =========================
def parse_directors(text: str) -> List[Dict[str, Any]]:
    log("Parsing Director/Signatory details…")

    mm = list(re.finditer(r"Director/Signatory Details|Director/Signatory details", text, flags=re.I))
    if not mm:
        log("No director section found.")
        return []

    sub = text[mm[-1].end():]
    end = re.search(r"(Disclaimer|Follow\s*us|This site is owned|Quick Links)", sub, flags=re.I)
    if end:
        sub = sub[:end.start()]

    # break into lines; keep non-empty
    raw_lines = [ln.strip() for ln in sub.split("\n") if ln.strip()]

    # remove common table headers
    header_pat = re.compile(
        r"^(Sr\.?|No|DIN/PAN|Name|Designation|Category|Date of|Appointment|Cessation|Signatory)$",
        re.I
    )
    raw_lines = [ln for ln in raw_lines if not header_pat.fullmatch(ln)]

    # expand lines like: "1 11405655 KATHAN ..." => ["11405655", "KATHAN ..."]
    expanded: List[str] = []
    for ln in raw_lines:
        ln = re.sub(r"\s{2,}", " ", ln).strip()
        m = re.match(r"^\s*\d+\s+(\d{8,12})\s+(.*)$", ln)
        if m:
            expanded.append(m.group(1))
            if m.group(2).strip():
                expanded.append(m.group(2).strip())
            continue
        # sometimes "1 11405655" only
        m2 = re.match(r"^\s*\d+\s+(\d{8,12})\s*$", ln)
        if m2:
            expanded.append(m2.group(1))
            continue
        # sometimes line starts with DIN directly but has extra
        m3 = re.match(r"^\s*(\d{8,12})\s+(.*)$", ln)
        if m3:
            expanded.append(m3.group(1))
            if m3.group(2).strip():
                expanded.append(m3.group(2).strip())
            continue

        expanded.append(ln)

    lines = expanded

    is_din = lambda s: bool(re.fullmatch(r"\d{8,12}", s))
    is_yesno = lambda s: bool(re.fullmatch(r"(Yes|No)", s, re.I))
    is_date = lambda s: bool(is_date_like(s))
    desig_pat = re.compile(r"^(Director|Managing Director|Nominee Director|Additional Director|Partner|Designated Partner)$", re.I)

    directors: List[Dict[str, Any]] = []
    i, L = 0, len(lines)

    while i < L:
        if not is_din(lines[i]):
            i += 1
            continue

        din_start = i
        while i < L and is_din(lines[i]):
            i += 1
        din_list = lines[din_start:i]
        N = len(din_list)
        if N == 0:
            continue

        # Gather name tokens until we hit designation or a new din block
        name_tokens, j = [], i
        while j < L and not desig_pat.fullmatch(lines[j]):
            if is_din(lines[j]) and name_tokens:
                break
            if not header_pat.fullmatch(lines[j]):
                name_tokens.append(lines[j])
            j += 1

        # split name tokens into N buckets (best-effort)
        names: List[str] = []
        q, r = divmod(len(name_tokens), N)
        cursor = 0
        for k in range(N):
            take = q + (1 if k < r else 0)
            chunk = name_tokens[cursor:cursor + take]
            cursor += take
            nm = " ".join(chunk).strip()
            names.append(nm.title() if nm else "")

        i = j

        desigs, cats, appts = [], [], []
        for k in range(N):
            if i < L and desig_pat.fullmatch(lines[i]):
                desigs.append(lines[i]); i += 1
            else:
                desigs.append(None)

            # category token (Promoter / Professional / etc.) often a single word
            if i < L and not (desig_pat.fullmatch(lines[i]) or is_date(lines[i]) or is_yesno(lines[i]) or is_din(lines[i]) or header_pat.fullmatch(lines[i])):
                cats.append(lines[i]); i += 1
            else:
                cats.append(None)

            if i < L and is_date(lines[i]):
                appts.append(lines[i]); i += 1
            else:
                appts.append(None)

        cess = []
        for k in range(N):
            if i < L and is_date(lines[i]):
                cess.append(lines[i]); i += 1
            else:
                cess.append(None)

        signs = []
        for k in range(N):
            if i < L and is_yesno(lines[i]):
                signs.append(lines[i]); i += 1
            else:
                signs.append(None)

        for k in range(N):
            directors.append({
                "din": din_list[k],
                "name": (names[k] or "").strip(),
                "designation": desigs[k],
                "category": cats[k],
                "date_of_appointment": appts[k],
                "cessation_date": None if (cess[k] in (None, "-")) else cess[k],
                "is_signatory": True if isinstance(signs[k], str) and signs[k].strip().lower() == "yes" else False
            })

    log(f"Directors parsed: {len(directors)}")
    for d in directors:
        log(f"  {d['din']} | {d['name']} | {d.get('designation')} | {d.get('category')} | {d.get('date_of_appointment')} | sign={d.get('is_signatory')}")
    return directors

# =========================
# SECTION 7: Write outputs
# =========================
def write_outputs(parsed: Dict[str, Any], clean_text: str, source_path: str) -> Dict[str, str]:
    company_hint = path_company_hint(source_path)
    company_name = company_hint or parsed.get("master_data", {}).get("company_name") or "UNKNOWN COMPANY"

    company_folder = COMPANIES_ROOT / safe_folderify(company_name)
    outputs_folder = company_folder / "outputs"
    ensure_dir(outputs_folder)

    tag = now_tag()
    json_path = outputs_folder / f"mca_extract_{tag}.json"
    txt_path = outputs_folder / f"mca_extract_{tag}.txt"

    log(f"[paths] BASE_DIR={BASE_DIR}")
    log(f"[paths] COMPANIES_ROOT={COMPANIES_ROOT}")
    log(f"[paths] Company folder={company_folder}")

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
    log(f"Started MCA extraction for: {path}")
    log(f"[paths] BASE_DIR={BASE_DIR}")
    log(f"[paths] COMPANIES_ROOT={COMPANIES_ROOT}")

    file_hash = sha256_of_file(path)
    log(f"File SHA-256: {file_hash}")

    raw = load_text_from_any(path)
    clean = normalize_text(raw)

    if not clean.strip():
        log("No text could be extracted. Exiting.")
        return {"status": "FAILED", "message": "No text extracted", "file": path, "file_hash": file_hash}

    master = parse_master_data(clean, pdf_path=path if Path(path).suffix.lower() == ".pdf" else None)
    directors = parse_directors(clean)

    parsed = {
        "source_file": path,
        "file_hash_sha256": file_hash,
        "parse_meta": {"parser": "p01_mca_extract.py", "version": "1.3.0", "timestamp": now_ts()},
        "master_data": master,
        "directors": directors,
        "raw_blocks": {"first_10k": clean[:10000]}
    }

    outs = write_outputs(parsed, clean, path)
    log("Extraction complete.")
    log(f"JSON: {outs['json']}")
    log(f"Text: {outs['text']}")

    return {
        "status": "SUCCESS",
        "message": "Extraction complete",
        "outputs": outs,
        "company_name": master.get("company_name"),
        "cin": master.get("cin")
    }

# =========================
# SECTION 9: CLI
# =========================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Extract MCA master data & directors from PDF/image.")
    ap.add_argument("--file", required=True, help="Path to MCA file")
    args = ap.parse_args()
    res = run(args.file)
    print("\n=== RESULT ===")
    print(json.dumps(res, indent=2, ensure_ascii=False))
