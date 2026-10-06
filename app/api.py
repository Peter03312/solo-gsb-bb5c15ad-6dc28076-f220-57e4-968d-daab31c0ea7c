"""JSON API 路由。

所有写操作在 service 层完成后统一 commit；唯一约束等竞态由 IntegrityError
兜底，转换为 409 冲突并给出明确提示。
"""

import csv
import io
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .enums import ACTION_LABELS, CATEGORY_LABELS
from .errors import Conflict
from .models import Sample
from . import schemas, services

router = APIRouter(prefix="/api", tags=["api"])


def _integrity_to_conflict(exc: IntegrityError) -> Conflict:
    msg = str(exc.orig)
    if "ux_handover_pending_per_sample" in msg:
        return Conflict("该样品已有待确认交接（并发请求仅一个生效），请刷新后重试。", "pending_exists")
    if "ux_open_isolation_per_sample" in msg:
        return Conflict("该样品已处于隔离中（并发隔离仅一个生效）。", "already_isolated")
    if "samples_sample_no_key" in msg:
        return Conflict("样品编号已存在（并发录入），请更换编号后重试。", "duplicate_sample_no")
    return Conflict(f"数据约束冲突，操作未生效：{msg[:200]}", "constraint")


# ---------- 人员 ----------
@router.get("/staff", response_model=list[schemas.StaffOut])
def staff_list(db: Session = Depends(get_db)):
    return services.list_staff(db)


@router.post("/staff", response_model=schemas.StaffOut, status_code=201)
def staff_create(payload: schemas.StaffCreate, db: Session = Depends(get_db)):
    try:
        staff = services.create_staff(db, payload.name, payload.role)
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise _integrity_to_conflict(e)
    db.refresh(staff)
    return staff


# ---------- 样品 ----------
@router.get("/samples", response_model=list[schemas.SampleOut])
def samples_list(db: Session = Depends(get_db)):
    return db.scalars(select(Sample).order_by(Sample.id.desc())).all()


@router.post("/samples", response_model=schemas.SampleOut, status_code=201)
def samples_create(payload: schemas.SampleCreate, db: Session = Depends(get_db)):
    try:
        sample = services.create_sample(
            db,
            sample_no=payload.sample_no.strip(),
            name=payload.name.strip(),
            temp_min=payload.temp_min,
            temp_max=payload.temp_max,
            location=payload.location.strip(),
            created_by_id=payload.created_by_id,
        )
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise _integrity_to_conflict(e)
    db.refresh(sample)
    return sample


@router.get("/samples/{sample_id}", response_model=schemas.SampleOut)
def sample_detail(sample_id: int, db: Session = Depends(get_db)):
    return services.get_sample(db, sample_id)


@router.get("/samples/{sample_id}/timeline")
def sample_timeline(sample_id: int, db: Session = Depends(get_db)):
    sample = services.get_sample(db, sample_id)
    return {"sample_id": sample.id, "timeline": services.build_timeline(db, sample)}


# ---------- 交接 ----------
@router.get("/handovers", response_model=list[schemas.HandoverOut])
def handovers_list(
    sample_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return services.list_handovers(db, sample_id)


@router.get("/handovers/{handover_id}", response_model=schemas.HandoverOut)
def handover_detail(handover_id: int, db: Session = Depends(get_db)):
    return services.get_handover(db, handover_id)


@router.post("/samples/{sample_id}/handovers", response_model=schemas.HandoverOut, status_code=201)
def handover_create(
    sample_id: int, payload: schemas.HandoverCreate, db: Session = Depends(get_db)
):
    try:
        handover = services.create_handover(
            db,
            sample_id=sample_id,
            giver_id=payload.giver_id,
            receiver_id=payload.receiver_id,
            destination=payload.destination.strip(),
            note=payload.note.strip(),
        )
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise _integrity_to_conflict(e)
    db.refresh(handover)
    return handover


@router.post("/handovers/{handover_id}/confirm", response_model=schemas.HandoverOut)
def handover_confirm(
    handover_id: int,
    operator_id: int = Query(..., description="确认人，必须是该单的接收人"),
    db: Session = Depends(get_db),
):
    try:
        handover = services.confirm_handover(
            db, handover_id=handover_id, operator_id=operator_id
        )
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise _integrity_to_conflict(e)
    db.refresh(handover)
    return handover


@router.post("/handovers/{handover_id}/cancel", response_model=schemas.HandoverOut)
def handover_cancel(
    handover_id: int,
    operator_id: int = Query(..., description="取消人，必须是该单的交出人"),
    db: Session = Depends(get_db),
):
    handover = services.cancel_handover(db, handover_id=handover_id, operator_id=operator_id)
    db.commit()
    db.refresh(handover)
    return handover


# ---------- 温度读数 ----------
@router.get("/samples/{sample_id}/readings", response_model=list[schemas.ReadingOut])
def readings_list(sample_id: int, db: Session = Depends(get_db)):
    services.get_sample(db, sample_id)
    return services.list_readings(db, sample_id)


@router.post("/samples/{sample_id}/readings", response_model=schemas.ReadingRecordResult, status_code=201)
def reading_create(
    sample_id: int, payload: schemas.ReadingCreate, db: Session = Depends(get_db)
):
    reading, isolation, invalidated = services.record_reading(
        db,
        sample_id=sample_id,
        value=payload.value,
        recorder_id=payload.recorder_id,
        note=payload.note.strip(),
        expected_version=payload.expected_version,
    )
    db.commit()
    db.refresh(reading)
    message = ""
    if not reading.in_range:
        parts = ["温度超出允许温区，样品已立即隔离"]
        if isolation is None:
            parts[0] = "温度超出允许温区（样品此前已处于隔离中）"
        if invalidated:
            parts.append(f"{len(invalidated)} 个待确认交接已失效")
        message = "；".join(parts) + "。"
    return {
        "id": reading.id,
        "sample_id": reading.sample_id,
        "value": float(reading.value),
        "in_range": reading.in_range,
        "recorder_id": reading.recorder_id,
        "recorded_at": reading.recorded_at,
        "note": reading.note,
        "corrects_reading_id": reading.corrects_reading_id,
        "correction_reason": reading.correction_reason,
        "correction_operator_id": reading.correction_operator_id,
        "isolation_opened": isolation is not None,
        "invalidated_handover_ids": invalidated,
        "message": message,
    }


@router.post("/readings/{reading_id}/correct", response_model=schemas.ReadingOut, status_code=201)
def reading_correct(
    reading_id: int, payload: schemas.ReadingCorrect, db: Session = Depends(get_db)
):
    correction = services.correct_reading(
        db,
        reading_id=reading_id,
        new_value=payload.new_value,
        reason=payload.reason.strip(),
        operator_id=payload.operator_id,
    )
    db.commit()
    db.refresh(correction)
    return correction


# ---------- 复核 / 隔离 ----------
@router.get("/samples/{sample_id}/isolations")
def isolations_list(sample_id: int, db: Session = Depends(get_db)):
    services.get_sample(db, sample_id)
    rows = services.list_isolations(db, sample_id)
    return [
        {
            "id": i.id,
            "sample_id": i.sample_id,
            "reason": i.reason,
            "reading_id": i.reading_id,
            "opened_by_id": i.opened_by_id,
            "opened_at": i.opened_at,
            "released_at": i.released_at,
            "released_by_id": i.released_by_id,
        }
        for i in rows
    ]


@router.post("/samples/{sample_id}/reviews", response_model=schemas.ReviewOut, status_code=201)
def review_create(
    sample_id: int, payload: schemas.ReviewCreate, db: Session = Depends(get_db)
):
    try:
        review = services.create_review(
            db,
            sample_id=sample_id,
            reviewer_id=payload.reviewer_id,
            result=payload.result,
            reason=payload.reason.strip(),
            expected_version=payload.expected_version,
        )
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise _integrity_to_conflict(e)
    db.refresh(review)
    return review


# ---------- 审计 ----------
def _audit_dict(a) -> dict:
    return {
        "id": a.id,
        "action": a.action,
        "action_label": ACTION_LABELS.get(a.action, a.action),
        "category": a.category,
        "category_label": CATEGORY_LABELS.get(a.category, a.category),
        "sample_id": a.sample_id,
        "actor_id": a.actor_id,
        "detail": a.detail,
        "created_at": a.created_at.isoformat(),
    }


@router.get("/audit")
def audit_list(
    sample_id: int | None = Query(default=None),
    exceptions_only: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    rows = services.list_audits(
        db, sample_id=sample_id, exceptions_only=exceptions_only, limit=limit
    )
    return [_audit_dict(a) for a in rows]


@router.get("/audit/export")
def audit_export(
    fmt: str = Query(default="csv", pattern="^(csv|json)$"),
    sample_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """导出完整审计记录（默认全部样品）。"""
    rows = services.list_audits(db, sample_id=sample_id, exceptions_only=False, limit=5000)
    data = [_audit_dict(a) for a in rows]

    if fmt == "json":
        content = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        media = "application/json"
        ext = "json"
    else:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["id", "时间(UTC)", "动作", "类别", "样品ID", "操作者ID", "详情(JSON)"]
        )
        for d in data:
            writer.writerow(
                [
                    d["id"],
                    d["created_at"],
                    d["action_label"],
                    d["category_label"],
                    d["sample_id"] or "",
                    d["actor_id"] or "",
                    json.dumps(d["detail"], ensure_ascii=False, default=str),
                ]
            )
        content = buf.getvalue()
        media = "text/csv; charset=utf-8"
        ext = "csv"

    filename = f"audit_logs.{ext}"
    return StreamingResponse(
        iter([content]),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
