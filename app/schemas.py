from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import HandoverStatus, ReviewResult, SampleStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Staff ----------
class StaffCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="工作人员", min_length=1, max_length=80)


class StaffOut(ORMModel):
    id: int
    name: str
    role: str
    active: bool
    created_at: datetime


# ---------- Sample ----------
class SampleCreate(BaseModel):
    sample_no: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=160)
    temp_min: Decimal
    temp_max: Decimal
    location: str = Field(min_length=1, max_length=160)
    created_by_id: int

    @model_validator(mode="after")
    def _check_range(self):
        if not self.temp_min.is_finite() or not self.temp_max.is_finite():
            raise ValueError("温度必须为有限数值")
        if self.temp_min >= self.temp_max:
            raise ValueError("允许温区下限必须小于上限")
        return self


class SampleOut(ORMModel):
    id: int
    sample_no: str
    name: str
    temp_min: float
    temp_max: float
    location: str
    status: SampleStatus
    version: int
    created_at: datetime
    created_by_id: int | None = None


# ---------- Handover ----------
class HandoverCreate(BaseModel):
    giver_id: int
    receiver_id: int
    destination: str = Field(min_length=1, max_length=160)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _check_persons(self):
        if self.giver_id == self.receiver_id:
            raise ValueError("交出人与接收人不能是同一人")
        return self


class HandoverOut(ORMModel):
    id: int
    sample_id: int
    giver_id: int
    receiver_id: int
    destination: str
    from_location: str
    status: HandoverStatus
    sample_version: int
    note: str
    created_at: datetime
    confirmed_at: datetime | None = None
    invalidated_at: datetime | None = None
    cancelled_at: datetime | None = None


# ---------- Readings ----------
class ReadingCreate(BaseModel):
    value: Decimal
    recorder_id: int
    note: str = Field(default="", max_length=2000)
    # 前端携带样品当前版本；不一致直接按过期操作拒绝
    expected_version: int | None = None

    @field_validator("value")
    @classmethod
    def _finite(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("温度读数必须为有限数值")
        return v


class ReadingCorrect(BaseModel):
    new_value: Decimal
    reason: str = Field(min_length=1, max_length=2000)
    operator_id: int

    @field_validator("new_value")
    @classmethod
    def _finite(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("温度读数必须为有限数值")
        return v


class ReadingOut(ORMModel):
    id: int
    sample_id: int
    value: float
    in_range: bool
    recorder_id: int
    recorded_at: datetime
    note: str
    corrects_reading_id: int | None = None
    correction_reason: str | None = None
    correction_operator_id: int | None = None


class ReadingRecordResult(ReadingOut):
    # 本次越界处理结果（仅记录读数接口返回）
    isolation_opened: bool = False
    invalidated_handover_ids: list[int] = []
    message: str = ""


# ---------- Review ----------
class ReviewCreate(BaseModel):
    reviewer_id: int
    result: ReviewResult
    reason: str = Field(min_length=1, max_length=2000)
    expected_version: int | None = None


class ReviewOut(ORMModel):
    id: int
    isolation_id: int
    reviewer_id: int
    result: ReviewResult
    reason: str
    created_at: datetime


# ---------- Audit ----------
class AuditOut(ORMModel):
    id: int
    action: str
    category: str
    sample_id: int | None
    actor_id: int | None
    detail: dict
    created_at: datetime
