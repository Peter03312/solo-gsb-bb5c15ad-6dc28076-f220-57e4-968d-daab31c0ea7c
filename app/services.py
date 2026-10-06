"""业务逻辑层。

并发控制约定（PostgreSQL）：
- 所有改变样品状态的操作先对 samples 行执行 SELECT ... FOR UPDATE；
- 交接的确认/取消先锁定 handovers 行，再锁定 samples 行；
- 样品带 version 字段，前端表单回传 expected_version，不一致即按“过期操作”拒绝；
- 唯一部分索引兜底：同一样品仅允许一个待确认交接、一条未解除隔离。
"""

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import SessionLocal
from .enums import HandoverStatus, ReviewResult, SampleStatus, STATUS_LABELS
from .errors import Conflict, NotFound, PermissionDenied
from .models import (
    AuditLog,
    Handover,
    Isolation,
    Review,
    Sample,
    Staff,
    TemperatureReading,
    utcnow,
)


def to_float(value) -> float | None:
    return float(value) if value is not None else None


# ---------- 审计 ----------
def log_audit(
    db: Session,
    action: str,
    category: str = "info",
    *,
    sample_id: int | None = None,
    actor_id: int | None = None,
    detail: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        category=category,
        sample_id=sample_id,
        actor_id=actor_id,
        detail=detail or {},
    )
    db.add(entry)
    return entry


def log_conflict_committed(
    action: str,
    *,
    sample_id: int | None,
    actor_id: int | None,
    detail: dict,
) -> None:
    """冲突/拒绝事件必须留下痕迹，但主事务需要回滚——用独立短事务提交。"""
    with SessionLocal() as audit_db:
        audit_db.add(
            AuditLog(
                action=action,
                category="conflict",
                sample_id=sample_id,
                actor_id=actor_id,
                detail=detail,
            )
        )
        audit_db.commit()


# ---------- 基础查询 ----------
def get_staff(db: Session, staff_id: int | None, *, require_active: bool = True) -> Staff:
    if staff_id is None:
        raise NotFound("未指定操作者")
    staff = db.get(Staff, staff_id)
    if staff is None:
        raise NotFound(f"人员 #{staff_id} 不存在")
    if require_active and not staff.active:
        raise Conflict(f"人员 {staff.name} 已停用，不能执行该操作", "inactive_staff")
    return staff


def check_version(sample: Sample, expected: int | None) -> None:
    if expected is not None and expected != sample.version:
        raise Conflict(
            f"操作基于过期状态（页面版本 v{expected}，当前 v{sample.version}）。"
            "请刷新页面查看最新状态后重试。",
            "stale_version",
        )


def lock_sample(db: Session, sample_id: int) -> Sample:
    sample = db.get(Sample, sample_id, with_for_update=True)
    if sample is None:
        raise NotFound(f"样品 #{sample_id} 不存在")
    return sample


# ---------- 人员 ----------
def list_staff(db: Session) -> Sequence[Staff]:
    return db.scalars(select(Staff).order_by(Staff.id)).all()


def create_staff(db: Session, name: str, role: str) -> Staff:
    staff = Staff(name=name, role=role)
    db.add(staff)
    log_audit(db, "staff_created", "info", actor_id=None, detail={"name": name, "role": role})
    return staff


# ---------- 样品 ----------
def get_sample(db: Session, sample_id: int) -> Sample:
    sample = db.get(Sample, sample_id)
    if sample is None:
        raise NotFound(f"样品 #{sample_id} 不存在")
    return sample


def create_sample(
    db: Session,
    *,
    sample_no: str,
    name: str,
    temp_min: Decimal,
    temp_max: Decimal,
    location: str,
    created_by_id: int,
) -> Sample:
    creator = get_staff(db, created_by_id)
    if db.scalar(select(Sample.id).where(Sample.sample_no == sample_no)):
        raise Conflict(f"样品编号 {sample_no} 已存在", "duplicate_sample_no")

    sample = Sample(
        sample_no=sample_no,
        name=name,
        temp_min=temp_min,
        temp_max=temp_max,
        location=location,
        status=SampleStatus.NORMAL,
        version=1,
        created_by_id=creator.id,
    )
    db.add(sample)
    db.flush()
    log_audit(
        db,
        "sample_created",
        "info",
        sample_id=sample.id,
        actor_id=creator.id,
        detail={
            "sample_no": sample_no,
            "name": name,
            "temp_min": str(temp_min),
            "temp_max": str(temp_max),
            "location": location,
        },
    )
    return sample


def list_samples_overview(db: Session) -> list[dict]:
    samples = list(db.scalars(select(Sample).order_by(Sample.id.desc())).all())

    # 每个样品最新读数（DISTINCT ON）
    latest_rows = db.scalars(
        select(TemperatureReading)
        .distinct(TemperatureReading.sample_id)
        .order_by(
            TemperatureReading.sample_id,
            TemperatureReading.recorded_at.desc(),
            TemperatureReading.id.desc(),
        )
    ).all()
    latest_by_sample = {r.sample_id: r for r in latest_rows}

    pending = db.scalars(
        select(Handover).where(Handover.status == HandoverStatus.PENDING)
    ).all()
    pending_by_sample: dict[int, Handover] = {h.sample_id: h for h in pending}
    staff_ids = {h.receiver_id for h in pending}
    staff_names = {s.id: s.name for s in db.scalars(select(Staff)).all() if s.id in staff_ids}

    result = []
    for s in samples:
        r = latest_by_sample.get(s.id)
        h = pending_by_sample.get(s.id)
        result.append(
            {
                "id": s.id,
                "sample_no": s.sample_no,
                "name": s.name,
                "temp_min": to_float(s.temp_min),
                "temp_max": to_float(s.temp_max),
                "location": s.location,
                "status": s.status,
                "version": s.version,
                "created_at": s.created_at,
                "latest_value": to_float(r.value) if r else None,
                "latest_in_range": r.in_range if r else None,
                "latest_at": r.recorded_at if r else None,
                "pending_to": h.destination if h else None,
                "pending_receiver": staff_names.get(h.receiver_id) if h else None,
            }
        )
    return result


# ---------- 交接 ----------
def get_handover(db: Session, handover_id: int) -> Handover:
    h = db.get(Handover, handover_id)
    if h is None:
        raise NotFound(f"交接 #{handover_id} 不存在")
    return h


def list_handovers(db: Session, sample_id: int | None = None) -> Sequence[Handover]:
    stmt = select(Handover).order_by(Handover.created_at.desc(), Handover.id.desc())
    if sample_id is not None:
        stmt = stmt.where(Handover.sample_id == sample_id)
    return db.scalars(stmt).all()


def create_handover(
    db: Session,
    *,
    sample_id: int,
    giver_id: int,
    receiver_id: int,
    destination: str,
    note: str,
) -> Handover:
    giver = get_staff(db, giver_id)
    receiver = get_staff(db, receiver_id)
    if giver.id == receiver.id:
        raise Conflict("交出人与接收人不能是同一人", "same_person")

    sample = lock_sample(db, sample_id)
    if sample.status == SampleStatus.ISOLATED:
        raise Conflict(
            f"样品 {sample.sample_no} 正处于隔离中，隔离期间禁止一切转移。",
            "isolated_transfer_blocked",
        )
    existing = db.scalar(
        select(Handover.id).where(
            Handover.sample_id == sample.id,
            Handover.status == HandoverStatus.PENDING,
        )
    )
    if existing:
        raise Conflict("该样品已有待确认交接，请先确认或取消后再发起。", "pending_exists")

    handover = Handover(
        sample_id=sample.id,
        giver_id=giver.id,
        receiver_id=receiver.id,
        destination=destination,
        from_location=sample.location,
        status=HandoverStatus.PENDING,
        sample_version=sample.version,
        note=note,
    )
    db.add(handover)
    db.flush()
    log_audit(
        db,
        "handover_created",
        "info",
        sample_id=sample.id,
        actor_id=giver.id,
        detail={
            "handover_id": handover.id,
            "from": handover.from_location,
            "to": destination,
            "giver": giver.name,
            "receiver": receiver.name,
            "note": note,
        },
    )
    return handover


def _handover_pending(h: Handover) -> None:
    if h.status == HandoverStatus.CONFIRMED:
        raise Conflict("该交接已确认，不能重复操作。", "already_confirmed")
    if h.status == HandoverStatus.INVALIDATED:
        raise Conflict(
            "该交接因样品温度越界被隔离而失效，不能再确认或取消。",
            "handover_invalidated",
        )
    if h.status == HandoverStatus.CANCELLED:
        raise Conflict("该交接已取消。", "handover_cancelled")


def confirm_handover(db: Session, *, handover_id: int, operator_id: int) -> Handover:
    handover = db.get(Handover, handover_id, with_for_update=True)
    if handover is None:
        raise NotFound(f"交接 #{handover_id} 不存在")
    _handover_pending(handover)

    operator = get_staff(db, operator_id)
    if operator.id != handover.receiver_id:
        log_conflict_committed(
            "confirm_rejected",
            sample_id=handover.sample_id,
            actor_id=operator.id,
            detail={
                "handover_id": handover.id,
                "reason": "非指定接收人尝试确认",
                "receiver_id": handover.receiver_id,
            },
        )
        raise PermissionDenied("只有交接单指定的接收人才能确认。")

    sample = lock_sample(db, handover.sample_id)

    # 样品在发起交接后被隔离（越界）：待确认交接立即失效，位置不变
    if sample.status == SampleStatus.ISOLATED:
        handover.status = HandoverStatus.INVALIDATED
        handover.invalidated_at = utcnow()
        sample.version += 1
        db.flush()
        log_audit(
            db,
            "transfer_rejected",
            "warning",
            sample_id=sample.id,
            actor_id=operator.id,
            detail={
                "handover_id": handover.id,
                "reason": "接收人确认时样品已隔离，位置保持不变，交接失效",
            },
        )
        log_audit(
            db,
            "handover_invalidated",
            "warning",
            sample_id=sample.id,
            actor_id=operator.id,
            detail={"handover_id": handover.id, "trigger": "confirm_during_isolation"},
        )
        db.commit()
        raise Conflict(
            "样品已处于隔离状态，本次确认无效，位置未改变，交接单已标记为失效。",
            "isolated_transfer_blocked",
        )

    # 确认生效：仅在此刻更新位置
    handover.status = HandoverStatus.CONFIRMED
    handover.confirmed_at = utcnow()
    sample.location = handover.destination
    sample.version += 1
    log_audit(
        db,
        "handover_confirmed",
        "info",
        sample_id=sample.id,
        actor_id=operator.id,
        detail={
            "handover_id": handover.id,
            "from": handover.from_location,
            "to": handover.destination,
        },
    )
    return handover


def cancel_handover(db: Session, *, handover_id: int, operator_id: int) -> Handover:
    handover = db.get(Handover, handover_id, with_for_update=True)
    if handover is None:
        raise NotFound(f"交接 #{handover_id} 不存在")
    _handover_pending(handover)

    operator = get_staff(db, operator_id)
    if operator.id != handover.giver_id:
        raise PermissionDenied("只有交接单的交出人可以取消。")

    handover.status = HandoverStatus.CANCELLED
    handover.cancelled_at = utcnow()
    log_audit(
        db,
        "handover_cancelled",
        "info",
        sample_id=handover.sample_id,
        actor_id=operator.id,
        detail={"handover_id": handover.id},
    )
    return handover


def _invalidate_pending_handovers(db: Session, sample_id: int, trigger: str) -> list[int]:
    pending = db.scalars(
        select(Handover)
        .where(Handover.sample_id == sample_id, Handover.status == HandoverStatus.PENDING)
        .with_for_update()
    ).all()
    ids = []
    for h in pending:
        h.status = HandoverStatus.INVALIDATED
        h.invalidated_at = utcnow()
        ids.append(h.id)
    return ids


# ---------- 温度读数 ----------
def list_readings(db: Session, sample_id: int) -> Sequence[TemperatureReading]:
    return db.scalars(
        select(TemperatureReading)
        .where(TemperatureReading.sample_id == sample_id)
        .order_by(TemperatureReading.recorded_at.desc(), TemperatureReading.id.desc())
    ).all()


def record_reading(
    db: Session,
    *,
    sample_id: int,
    value: Decimal,
    recorder_id: int,
    note: str,
    expected_version: int | None = None,
) -> tuple[TemperatureReading, Isolation | None, list[int]]:
    recorder = get_staff(db, recorder_id)
    sample = lock_sample(db, sample_id)
    check_version(sample, expected_version)

    in_range = Decimal(str(sample.temp_min)) <= value <= Decimal(str(sample.temp_max))
    reading = TemperatureReading(
        sample_id=sample.id,
        value=value,
        in_range=in_range,
        recorder_id=recorder.id,
        note=note,
    )
    db.add(reading)
    db.flush()

    isolation: Isolation | None = None
    invalidated_ids: list[int] = []

    log_audit(
        db,
        "reading_recorded",
        "info" if in_range else "exception",
        sample_id=sample.id,
        actor_id=recorder.id,
        detail={
            "reading_id": reading.id,
            "value": str(value),
            "in_range": in_range,
            "allowed": [to_float(sample.temp_min), to_float(sample.temp_max)],
            "note": note,
        },
    )

    if not in_range:
        # 越界：立即隔离 + 待确认交接失效（隔离期间同样适用）
        log_audit(
            db,
            "temp_excursion",
            "exception",
            sample_id=sample.id,
            actor_id=recorder.id,
            detail={
                "reading_id": reading.id,
                "value": str(value),
                "allowed": [to_float(sample.temp_min), to_float(sample.temp_max)],
                "already_isolated": sample.status == SampleStatus.ISOLATED,
            },
        )

        if sample.status != SampleStatus.ISOLATED:
            isolation = Isolation(
                sample_id=sample.id,
                reason=(
                    f"温度读数 {value} ℃ 超出允许温区 "
                    f"{to_float(sample.temp_min)} ~ {to_float(sample.temp_max)} ℃，系统自动隔离"
                ),
                reading_id=reading.id,
                opened_by_id=recorder.id,
            )
            db.add(isolation)
            db.flush()
            sample.status = SampleStatus.ISOLATED
            sample.version += 1
            log_audit(
                db,
                "isolation_opened",
                "exception",
                sample_id=sample.id,
                actor_id=recorder.id,
                detail={"isolation_id": isolation.id, "reading_id": reading.id},
            )

        invalidated_ids = _invalidate_pending_handovers(db, sample.id, trigger="temp_excursion")
        for hid in invalidated_ids:
            log_audit(
                db,
                "handover_invalidated",
                "warning",
                sample_id=sample.id,
                actor_id=recorder.id,
                detail={"handover_id": hid, "trigger": "temp_excursion"},
            )

    return reading, isolation, invalidated_ids


def correct_reading(
    db: Session,
    *,
    reading_id: int,
    new_value: Decimal,
    reason: str,
    operator_id: int,
) -> TemperatureReading:
    operator = get_staff(db, operator_id)
    original = db.get(TemperatureReading, reading_id)
    if original is None:
        raise NotFound(f"读数 #{reading_id} 不存在")
    if original.corrects_reading_id is not None or original.correction_reason is not None:
        raise Conflict("更正记录不能再次更正，请更正最初的原始读数。", "cannot_recorrect")

    sample = db.get(Sample, original.sample_id)
    in_range = Decimal(str(sample.temp_min)) <= new_value <= Decimal(str(sample.temp_max))

    # 新增一条更正读数，原始读数原样保留；绝不自动解除隔离
    correction = TemperatureReading(
        sample_id=original.sample_id,
        value=new_value,
        in_range=in_range,
        recorder_id=operator.id,
        note=f"对读数 #{original.id} 的更正",
        corrects_reading_id=original.id,
        correction_reason=reason,
        correction_operator_id=operator.id,
    )
    db.add(correction)
    db.flush()
    log_audit(
        db,
        "reading_corrected",
        "warning",
        sample_id=original.sample_id,
        actor_id=operator.id,
        detail={
            "original_reading_id": original.id,
            "original_value": str(original.value),
            "corrected_reading_id": correction.id,
            "new_value": str(new_value),
            "reason": reason,
            "isolation_status": sample.status.value,
            "auto_release": False,
        },
    )
    return correction


# ---------- 隔离 / 复核 ----------
def list_isolations(db: Session, sample_id: int) -> Sequence[Isolation]:
    return db.scalars(
        select(Isolation)
        .where(Isolation.sample_id == sample_id)
        .order_by(Isolation.opened_at.desc(), Isolation.id.desc())
    ).all()


def create_review(
    db: Session,
    *,
    sample_id: int,
    reviewer_id: int,
    result: ReviewResult,
    reason: str,
    expected_version: int | None = None,
) -> Review:
    reviewer = get_staff(db, reviewer_id)
    sample = lock_sample(db, sample_id)
    check_version(sample, expected_version)

    if sample.status != SampleStatus.ISOLATED:
        raise Conflict("样品当前未隔离，不能登记复核结论。", "not_isolated")

    isolation = db.scalar(
        select(Isolation)
        .where(Isolation.sample_id == sample.id, Isolation.released_at.is_(None))
        .with_for_update()
    )
    if isolation is None:
        raise Conflict("没有找到该样品的未解除隔离记录。", "no_open_isolation")

    review = Review(
        isolation_id=isolation.id,
        reviewer_id=reviewer.id,
        result=result,
        reason=reason,
    )
    db.add(review)
    db.flush()

    released = result == ReviewResult.RELEASE
    # 任何复核结论都推进样品版本：确保另一个停留在旧页面的复核/转移操作被判为过期
    sample.version += 1
    log_audit(
        db,
        "review_recorded",
        "warning",
        sample_id=sample.id,
        actor_id=reviewer.id,
        detail={
            "review_id": review.id,
            "isolation_id": isolation.id,
            "result": result.value,
            "reason": reason,
        },
    )
    if released:
        isolation.released_at = review.created_at
        isolation.released_by_id = reviewer.id
        sample.status = SampleStatus.NORMAL
        log_audit(
            db,
            "isolation_closed",
            "warning",
            sample_id=sample.id,
            actor_id=reviewer.id,
            detail={"isolation_id": isolation.id, "review_id": review.id, "reason": reason},
        )
    return review


# ---------- 审计查询 / 时间线 ----------
def list_audits(
    db: Session,
    *,
    sample_id: int | None = None,
    exceptions_only: bool = False,
    limit: int = 200,
) -> Sequence[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if sample_id is not None:
        stmt = stmt.where(AuditLog.sample_id == sample_id)
    if exceptions_only:
        stmt = stmt.where(AuditLog.category.in_(("exception", "warning", "conflict")))
    stmt = stmt.limit(limit)
    return db.scalars(stmt).all()


def build_timeline(db: Session, sample: Sample) -> list[dict]:
    """合并样品全部相关实体，生成统一流转时间线。"""
    staff = {s.id: s.name for s in db.scalars(select(Staff)).all()}
    handovers = list_handovers(db, sample.id)
    readings = list_readings(db, sample.id)
    isolations = list_isolations(db, sample.id)
    reviews = db.scalars(
        select(Review)
        .join(Isolation, Review.isolation_id == Isolation.id)
        .where(Isolation.sample_id == sample.id)
        .order_by(Review.created_at.desc(), Review.id.desc())
    ).all()

    def name(staff_id: int | None) -> str:
        return staff.get(staff_id, f"#{staff_id}") if staff_id is not None else "系统"

    entries: list[dict] = []

    entries.append(
        {
            "at": sample.created_at,
            "rank": 0,
            "category": "info",
            "title": "样品录入",
            "lines": [
                f"编号 {sample.sample_no}" + (f"（{sample.name}）" if sample.name else ""),
                f"允许温区 {to_float(sample.temp_min)} ~ {to_float(sample.temp_max)} ℃",
                f"初始位置：{sample.location}",
                f"录入人：{name(sample.created_by_id)}",
            ],
        }
    )

    for h in handovers:
        base = {
            "giver": name(h.giver_id),
            "receiver": name(h.receiver_id),
            "handover_id": h.id,
        }
        entries.append(
            {
                "at": h.created_at,
                "rank": 1,
                "category": "info",
                "title": f"发起交接：{h.from_location} → {h.destination}",
                "lines": [
                    f"交出人：{base['giver']}；接收人：{base['receiver']}",
                    f"确认前样品位置保持 {h.from_location} 不变",
                    f"备注：{h.note}" if h.note else "",
                ],
                "_handover_id": h.id,
            }
        )
        if h.status == HandoverStatus.CONFIRMED and h.confirmed_at:
            entries.append(
                {
                    "at": h.confirmed_at,
                    "rank": 8,
                    "category": "info",
                    "title": f"接收人确认，位置更新为 {h.destination}",
                    "lines": [f"交接 #{h.id} 已确认"],
                }
            )
        elif h.status == HandoverStatus.INVALIDATED and h.invalidated_at:
            entries.append(
                {
                    "at": h.invalidated_at,
                    "rank": 5,
                    "category": "warning",
                    "title": f"交接 #{h.id} 失效",
                    "lines": ["因样品温度越界隔离/确认时已隔离，待确认交接失效，位置未改变"],
                }
            )
        elif h.status == HandoverStatus.CANCELLED and h.cancelled_at:
            entries.append(
                {
                    "at": h.cancelled_at,
                    "rank": 6,
                    "category": "info",
                    "title": f"交接 #{h.id} 已取消",
                    "lines": [],
                }
            )

    for r in readings:
        flag = "符合温区" if r.in_range else "超出允许温区"
        if r.corrects_reading_id is not None:
            entries.append(
                {
                    "at": r.recorded_at,
                    "rank": 2,
                    "category": "warning",
                    "title": f"读数更正：#{r.corrects_reading_id} → {to_float(r.value)} ℃",
                    "lines": [
                        f"操作者：{name(r.correction_operator_id)}",
                        f"更正理由：{r.correction_reason}",
                        f"原始读数 #{r.corrects_reading_id} 原样保留；隔离状态不因此自动改变",
                        f"新读数判定：{flag}",
                    ],
                }
            )
        else:
            lines = [
                f"记录人：{name(r.recorder_id)}",
                f"判定：{flag}（允许 {to_float(sample.temp_min)} ~ {to_float(sample.temp_max)} ℃）",
            ]
            if r.note:
                lines.append(f"备注：{r.note}")
            entries.append(
                {
                    "at": r.recorded_at,
                    "rank": 2,
                    "category": "info" if r.in_range else "exception",
                    "title": f"温度记录 {to_float(r.value)} ℃",
                    "lines": lines,
                }
            )

    for iso in isolations:
        entries.append(
            {
                "at": iso.opened_at,
                "rank": 3,
                "category": "exception",
                "title": "隔离开启（禁止转移）",
                "lines": [iso.reason, f"操作人：{name(iso.opened_by_id)}"],
            }
        )
        if iso.released_at:
            entries.append(
                {
                    "at": iso.released_at,
                    "rank": 4,
                    "category": "warning",
                    "title": "隔离解除",
                    "lines": [f"解除人：{name(iso.released_by_id)}（依据复核结论）"],
                }
            )

    for rv in reviews:
        lines = [f"复核人：{name(rv.reviewer_id)}", f"理由：{rv.reason}"]
        if rv.result == ReviewResult.RELEASE:
            lines.append("结论：解除隔离，样品恢复正常流转")
        else:
            lines.append("结论：维持隔离，样品继续禁止转移")
        entries.append(
            {
                "at": rv.created_at,
                "rank": 4,
                "category": "warning",
                "title": STATUS_LABELS[rv.result],
                "lines": lines,
            }
        )

    # 冲突/拒绝等仅存在于审计表中的事件
    extra_actions = {"state_conflict", "confirm_rejected", "transfer_rejected"}
    audits = db.scalars(
        select(AuditLog)
        .where(AuditLog.sample_id == sample.id, AuditLog.action.in_(extra_actions))
        .order_by(AuditLog.created_at.desc())
    ).all()
    for a in audits:
        d = a.detail or {}
        entries.append(
            {
                "at": a.created_at,
                "rank": 7,
                "category": "conflict",
                "title": f"操作被拒绝：{d.get('reason', a.action)}",
                "lines": [f"操作人：{name(a.actor_id)}"],
            }
        )

    entries.sort(key=lambda e: (e["at"], e["rank"]), reverse=True)
    for e in entries:
        e.pop("rank", None)
        e.pop("_handover_id", None)
        e["lines"] = [ln for ln in e["lines"] if ln]
    return entries
