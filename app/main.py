# app/main.py
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRoute

from .settings import COMPANIES_ROOT, FRONTEND_ROOT

# Import routers ONCE
from .routers import master, templates_word, mail  # mail sending API 
from .routers import companies  # companies list API
from .routers import mail_sla   # add this import

 # add this line after your other include_router calls
from .routers import mail_payment_onboarding, mail_proposal 
from .routers import mail_adt1


from .routers import mail_adt1_ack
# app/main.py (add with other router imports)
from .routers import inc20a

# ... after app = FastAPI(...)
  # prefix already defined in the router

from .routers import mail_gst_init


from .routers import gst_authorized_signatory
from .routers import mail_gst_signatory

from .routers import mail_udyam

from .routers import mail_commencement_inc20a
from .routers import share_certificate
from .routers import mail_share_certificate_issue


app = FastAPI(title="Doc Pack Backend (Minimal)", version="1.0.0")

# ❗ Routers ALREADY have their own prefixes inside each file.
#    Include them WITHOUT extra prefixes here.
app.include_router(master.router, prefix="/api/master", tags=["master"])    # master.py: APIRouter(prefix="/api/master", ...)
app.include_router(templates_word.router, prefix="/api/templates/word", tags=["templates-word"])  # templates_word.py: APIRouter(prefix="/api/templates/word", ...)
app.mount("/companies", StaticFiles(directory=str(COMPANIES_ROOT)), name="companies")
app.include_router(mail.router)
app.include_router(mail_sla.router) 
app.include_router(mail_payment_onboarding.router)

# after app = FastAPI(...):
app.include_router(mail_proposal.router)  # <-- add

app.include_router(mail_adt1.router)
app.include_router(mail_adt1_ack.router)

# For companies, include according to how its router is defined.
# If companies.router exposes GET "/" to list companies, keep this prefix to serve GET /api/companies:
app.include_router(companies.router, prefix="/api/companies", tags=["companies"])
app.include_router(inc20a.router) 
app.include_router(mail_gst_init.router)
app.include_router(gst_authorized_signatory.router)
app.include_router(gst_authorized_signatory.compat) 
app.include_router(mail_gst_signatory.router)
app.include_router(mail_udyam.router)
app.include_router(mail_commencement_inc20a.router)
app.include_router(share_certificate.router)
app.include_router(mail_share_certificate_issue.router)


# Static mounts
app.mount("/companies", StaticFiles(directory=str(COMPANIES_ROOT)), name="companies")
if FRONTEND_ROOT.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_ROOT), html=True), name="frontend")

@app.get("/")
def root():
    return {
        "status": "ok",
        "routes": [r.path for r in app.routes if isinstance(r, APIRoute)],
        "try": ["/docs", "POST /api/master/generate"]
    }

@app.get("/app", include_in_schema=False)
def app_index_redirect():
    return RedirectResponse(url="/app/")
