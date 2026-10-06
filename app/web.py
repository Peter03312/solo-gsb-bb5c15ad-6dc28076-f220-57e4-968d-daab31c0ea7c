"""服务端渲染页面（操作本身全部走 /api，前端 JS 调用）。"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import get_db
from .enums import ReviewResult, SampleStatus, STATUS_LABELS
from . import services

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    staff = services.list_staff(db)
    overview = services.list_samples_overview(db)
    exceptions = services.list_audits(db, exceptions_only=True, limit=20)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "staff": staff,
            "overview": overview,
            "exceptions": exceptions,
            "status_labels": STATUS_LABELS,
        },
    )


@router.get("/samples/{sample_id}", response_class=HTMLResponse)
def sample_detail(sample_id: int, request: Request, db: Session = Depends(get_db)):
    sample = services.get_sample(db, sample_id)
    staff = services.list_staff(db)
    handovers = services.list_handovers(db, sample_id)
    readings = services.list_readings(db, sample_id)
    isolations = services.list_isolations(db, sample_id)
    timeline = services.build_timeline(db, sample)
    open_isolation = next((i for i in isolations if i.released_at is None), None)
    return templates.TemplateResponse(
        request,
        "sample_detail.html",
        {
            "sample": sample,
            "staff": {s.id: s for s in staff},
            "handovers": handovers,
            "readings": readings,
            "isolations": isolations,
            "open_isolation": open_isolation,
            "timeline": timeline,
            "status_labels": STATUS_LABELS,
            "review_results": list(ReviewResult),
            "SampleStatus": SampleStatus,
        },
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    audits = services.list_audits(db, exceptions_only=False, limit=200)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {"audits": audits},
    )
