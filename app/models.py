from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .enums import HandoverStatus, ReviewResult, SampleStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enum_values(pg_enum):
    """让 PostgreSQL 枚举标签使用枚举的小写 value，而非成员名。"""
    return [e.value for e in pg_enum]


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(80), nullable=False, default="工作人员")
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (
        CheckConstraint("temp_min < temp_max", name="ck_samples_temp_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    temp_min: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    temp_max: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    location: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[SampleStatus] = mapped_column(
        SAEnum(SampleStatus, name="sample_status", values_callable=enum_values),
        nullable=False,
        default=SampleStatus.NORMAL,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))

    created_by: Mapped[Staff | None] = relationship(foreign_keys=[created_by_id])


class Handover(Base):
    __tablename__ = "handovers"
    __table_args__ = (
        # 同一样品同时只允许一个待确认交接
        Index(
            "ux_handover_pending_per_sample",
            "sample_id",
            unique=True,
            postgresql_where="status = 'pending'",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), nullable=False)
    giver_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    receiver_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    destination: Mapped[str] = mapped_column(String(160), nullable=False)
    from_location: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[HandoverStatus] = mapped_column(
        SAEnum(
            HandoverStatus,
            name="handover_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=HandoverStatus.PENDING,
    )
    # 创建时样品快照版本；确认时必须与样品当前版本一致，否则判定为过期操作
    sample_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(nullable=True)

    sample: Mapped[Sample] = relationship()
    giver: Mapped[Staff] = relationship(foreign_keys=[giver_id])
    receiver: Mapped[Staff] = relationship(foreign_keys=[receiver_id])


class TemperatureReading(Base):
    __tablename__ = "temperature_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(7, 2), nullable=False)
    in_range: Mapped[bool] = mapped_column(nullable=False)
    recorder_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 更正：被更正的原始读数；correction_reason 非空表示本条是更正记录
    corrects_reading_id: Mapped[int | None] = mapped_column(
        ForeignKey("temperature_readings.id")
    )
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_operator_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))

    sample: Mapped[Sample] = relationship()
    recorder: Mapped[Staff] = relationship(foreign_keys=[recorder_id])
    correction_operator: Mapped[Staff | None] = relationship(
        foreign_keys=[correction_operator_id]
    )


class Isolation(Base):
    __tablename__ = "isolations"
    __table_args__ = (
        # 同一样品同时只允许一条未解除的隔离
        Index(
            "ux_open_isolation_per_sample",
            "sample_id",
            unique=True,
            postgresql_where="released_at IS NULL",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reading_id: Mapped[int | None] = mapped_column(ForeignKey("temperature_readings.id"))
    opened_by_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(nullable=True)
    released_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))

    sample: Mapped[Sample] = relationship()
    reading: Mapped[TemperatureReading | None] = relationship()
    opened_by: Mapped[Staff] = relationship(foreign_keys=[opened_by_id])
    released_by: Mapped[Staff | None] = relationship(foreign_keys=[released_by_id])
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="isolation", cascade="all, delete-orphan"
    )


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("char_length(reason) > 0", name="ck_reviews_reason"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    isolation_id: Mapped[int] = mapped_column(ForeignKey("isolations.id"), nullable=False)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    result: Mapped[ReviewResult] = mapped_column(
        SAEnum(
            ReviewResult,
            name="review_result",
            values_callable=enum_values,
        ),
        nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    isolation: Mapped[Isolation] = relationship(back_populates="reviews")
    reviewer: Mapped[Staff] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    sample_id: Mapped[int | None] = mapped_column(ForeignKey("samples.id"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    actor: Mapped[Staff | None] = relationship()
