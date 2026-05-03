# app/routers/share_certificate.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..settings import BACKEND_ROOT, COMPANIES_ROOT, TEMPLATES_WORD_ROOT
from ..utils.render_docx import _latest_master, build_context, render_docx

import json
import re

router = APIRouter(
    prefix="/api/share-cert",
    tags=["share_certificates"],
)

# ===== Helpers =====


def _safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", s.strip()).strip("_").lower()


def _company_folder(name: str) -> Path:
    p = COMPANIES_ROOT / name
    if not p.exists():
        raise HTTPException(404, f"Company folder not found: {p}")
    return p


def _download_url(abs_path: Path) -> str:
    rel = abs_path.relative_to(COMPANIES_ROOT).as_posix()
    return f"/companies/{rel}"


def _load_master_json(company_folder: Path) -> Dict[str, Any]:
    master_path = _latest_master(company_folder)
    if not master_path or not master_path.exists():
        raise HTTPException(
            404, f"Master JSON not found for company {company_folder.name}"
        )
    try:
        return json.loads(master_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, f"Failed to read master JSON: {exc}") from exc


# ===== Models =====


class ShareCertItem(BaseModel):
    certificate_no: str = Field(
        ..., description="Certificate number as printed on SH-1"
    )
    folio_no: Optional[str] = Field(None, description="Register folio number")
    shareholder_name: str
    shareholder_address: Optional[str] = None
    no_of_shares: int
    share_type: str = "Equity Shares"
    face_value: Optional[float] = None
    paid_up_per_share: Optional[float] = None
    distinctive_from: int
    distinctive_to: int
    date_of_issue: Optional[str] = None  # e.g. 2024-04-29 (from <input type=date>)
    place: Optional[str] = None  # e.g. Navi Mumbai


class GenerateShareCertRequest(BaseModel):
    company_key: str = Field(
        ..., description="Exact company folder name under COMPANIES_ROOT"
    )
    certificates: List[ShareCertItem]


# ===== API: directors list for dropdown =====


@router.get(
    "/directors",
    summary="List directors for a company for use in share certificate dropdown",
)
def list_directors(company_key: str = Query(..., alias="company_key")):
    """
    Returns directors from the latest _master_*.json for the given company.
    Used by frontend to populate the 'Director' dropdown per row.
    """
    cfolder = _company_folder(company_key)
    data = _load_master_json(cfolder)
    directors = data.get("directors") or []

    out = []
    for d in directors:
        if not isinstance(d, dict):
            continue
        out.append(
            {
                "name": d.get("name"),
                "din": d.get("din"),
                "designation": d.get("designation") or "Director",
                "is_signatory": bool(d.get("is_signatory", True)),
            }
        )

    return {
        "company_name": data.get("company_name") or company_key,
        "directors": out,
    }


# ===== API: generate SH-1 share certificates =====


@router.post(
    "/generate",
    summary="Generate Form SH-1 share certificates for a company",
)
def generate_share_certificates(payload: GenerateShareCertRequest):
    """
    Renders one DOCX per certificate using the SH-1 template.
    Files are saved in the respective company folder under COMPANIES_ROOT.
    """
    if not payload.certificates:
        raise HTTPException(400, "No certificates supplied")

    company_key = payload.company_key
    cfolder = _company_folder(company_key)

    # Base context from master (same helper as INC-20A, etc.)
    master_path = _latest_master(cfolder)
    ctx_base = build_context(master_path, explicit_date=None)
    if not isinstance(ctx_base, dict):
        ctx_base = dict(ctx_base or {})

    # Ensure directors present in context
    master_raw = _load_master_json(cfolder)
    directors = master_raw.get("directors") or ctx_base.get("directors") or []
    directors = list(directors or [])

    # ---- SAFETY BLOCK for OPC / 1-director / 0-director cases ----
    # If there is only 1 director (OPC), duplicate so template can safely access [1]
    if len(directors) == 1:
        directors.append(directors[0])
    elif len(directors) == 0:
        # Extreme safety: at least two empty dicts
        directors = [{"name": ""}, {"name": ""}]

    ctx_base["directors"] = directors

    # Build directors_pairs so template can access pairs without index errors
    directors_pairs: List[List[Dict[str, Any]]] = []
    for i in range(0, len(directors), 2):
        if i + 1 < len(directors):
            directors_pairs.append([directors[i], directors[i + 1]])
        else:
            # Odd count – duplicate last director
            directors_pairs.append([directors[i], directors[i]])

    ctx_base["directors_pairs"] = directors_pairs
    # ---- END SAFETY BLOCK ----

    # Locate SH-1 template
    tpl_dir = TEMPLATES_WORD_ROOT / "share_cert"
    tpl = tpl_dir / "SH template.docx"  # primary expected name
    if not tpl.exists():
        alt = tpl_dir / "Share_Certificate_SH1_PvtLtd.docx"  # fallback name
        if alt.exists():
            tpl = alt
        else:
            raise HTTPException(
                500,
                f"Share certificate template not found. "
                f"Expected one of: {tpl}, {alt}",
            )

    items: List[Dict[str, Any]] = []

    for cert in payload.certificates:
        sc = cert.dict()

        # Default paid_up_per_share = face_value if blank
        if sc.get("paid_up_per_share") is None and sc.get("face_value") is not None:
            sc["paid_up_per_share"] = sc["face_value"]

        # Build context for this certificate
        ctx = dict(ctx_base)
        ctx["share_cert"] = sc

        tpl_id = f"sharecert__{_safe(company_key)}__{_safe(cert.certificate_no)}"
        out_path = render_docx(tpl, cfolder, ctx, tpl_id)

        items.append(
            {
                "certificate_no": cert.certificate_no,
                "shareholder_name": cert.shareholder_name,
                "output_docx": str(out_path),
                "download_url": _download_url(out_path),
            }
        )

    return {
        "ok": True,
        "company_key": company_key,
        "count": len(items),
        "items": items,
    }
