# app/routers/companies.py
from fastapi import APIRouter
from pathlib import Path
from ..settings import COMPANIES_ROOT

router = APIRouter()

@router.get("")
def list_companies():
    root = Path(COMPANIES_ROOT)
    if not root.exists():
        return []
    return sorted([p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")])
