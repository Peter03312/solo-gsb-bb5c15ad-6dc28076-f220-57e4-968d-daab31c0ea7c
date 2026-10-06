"""数据库初始化：建表 + 初始人员（+ 可关闭的演示数据）。

由 entrypoint 在 web 服务启动前执行；重复执行安全（按名称/编号幂等）。
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, SessionLocal, engine
from .enums import HandoverStatus, ReviewResult, SampleStatus
from .models import Handover, Isolation, Review, Sample, Staff, TemperatureReading, utcnow
from . import services


def ensure_staff(db: Session, name: str, role: str) -> Staff:
    staff = db.scalar(select(Staff).where(Staff.name == name))
    if staff:
        return staff
    staff = Staff(name=name, role=role)
    db.add(staff)
    db.flush()
    services.log_audit(db, "staff_created", "info", actor_id=None, detail={"name": name, "role": role})
    return staff


def seed() -> None:
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        zhang = ensure_staff(db, "张工", "采样交出人")
        li = ensure_staff(db, "李工", "样本库接收人")
        wang = ensure_staff(db, "王工", "复核负责人")
        zhao = ensure_staff(db, "赵工", "温区巡检员")
        db.commit()

        if not settings.seed_demo_data:
            print("[bootstrap] 演示数据已关闭（SEED_DEMO_DATA=false）。")
            return

        ids = {}

        def ensure_sample(sample_no: str, **kwargs) -> Sample | None:
            s = db.scalar(select(Sample).where(Sample.sample_no == sample_no))
            if s:
                ids[sample_no] = s
                return None
            s = Sample(**kwargs, sample_no=sample_no, status=SampleStatus.NORMAL, version=1)
            db.add(s)
            db.flush()
            services.log_audit(
                db, "sample_created", "info", sample_id=s.id, actor_id=zhang.id,
                detail={"sample_no": sample_no, "location": s.location,
                        "temp_min": str(s.temp_min), "temp_max": str(s.temp_max)},
            )
            ids[sample_no] = s
            return s

        created_a = ensure_sample(
            "SMP-2026-001", name="血浆样本 A", temp_min=Decimal("2.0"),
            temp_max=Decimal("8.0"), location="A 库 3 号冷藏柜", created_by_id=zhang.id,
        )
        created_b = ensure_sample(
            "SMP-2026-002", name="组织样本 B", temp_min=Decimal("-25.0"),
            temp_max=Decimal("-15.0"), location="B 库深冷柜 7 号位", created_by_id=zhang.id,
        )
        created_c = ensure_sample(
            "SMP-2026-003", name="试剂对照 C", temp_min=Decimal("2.0"),
            temp_max=Decimal("25.0"), location="C 区试剂架 2 层", created_by_id=zhang.id,
        )
        db.commit()

        sa, sb, sc = ids["SMP-2026-001"], ids["SMP-2026-002"], ids["SMP-2026-003"]

        # A：一条已确认交接 + 正常读数
        if created_a:
            h = Handover(
                sample_id=sa.id, giver_id=zhang.id, receiver_id=li.id,
                destination="B 库交接暂存位", from_location=sa.location,
                status=HandoverStatus.CONFIRMED, sample_version=1,
                confirmed_at=utcnow(),
            )
            db.add(h)
            sa.location = h.destination
            sa.version = 2
            db.add(TemperatureReading(
                sample_id=sa.id, value=Decimal("5.4"), in_range=True, recorder_id=zhao.id,
                note="巡检记录",
            ))
            services.log_audit(db, "handover_created", "info", sample_id=sa.id, actor_id=zhang.id,
                               detail={"from": "A 库 3 号冷藏柜", "to": h.destination})
            services.log_audit(db, "handover_confirmed", "info", sample_id=sa.id, actor_id=li.id,
                               detail={"from": "A 库 3 号冷藏柜", "to": h.destination})
            services.log_audit(db, "reading_recorded", "info", sample_id=sa.id, actor_id=zhao.id,
                               detail={"value": "5.4", "in_range": True})

        # B：越界读数 -> 隔离 -> 待确认交接失效 -> 继续隔离复核一次
        if created_b:
            bad = TemperatureReading(
                sample_id=sb.id, value=Decimal("-9.8"), in_range=False, recorder_id=zhao.id,
                note="冷柜门未关严巡检",
            )
            db.add(bad)
            db.flush()
            pending = Handover(
                sample_id=sb.id, giver_id=zhang.id, receiver_id=li.id,
                destination="D 区实验台", from_location=sb.location,
                status=HandoverStatus.INVALIDATED, sample_version=1, invalidated_at=utcnow(),
            )
            iso = Isolation(
                sample_id=sb.id,
                reason="温度读数 -9.80 ℃ 超出允许温区 -25.00 ~ -15.00 ℃，系统自动隔离",
                reading_id=bad.id, opened_by_id=zhao.id,
            )
            db.add_all([pending, iso])
            db.flush()
            sb.status = SampleStatus.ISOLATED
            sb.version = 2
            rv = Review(
                isolation_id=iso.id, reviewer_id=wang.id,
                result=ReviewResult.CONTINUE_ISOLATION,
                reason="等待冷链设备维修确认，先维持隔离。",
            )
            db.add(rv)
            services.log_audit(db, "reading_recorded", "exception", sample_id=sb.id, actor_id=zhao.id,
                               detail={"value": "-9.8", "in_range": False})
            services.log_audit(db, "temp_excursion", "exception", sample_id=sb.id, actor_id=zhao.id,
                               detail={"value": "-9.8", "allowed": [-25.0, -15.0]})
            services.log_audit(db, "isolation_opened", "exception", sample_id=sb.id, actor_id=zhao.id,
                               detail={"isolation_id": iso.id})
            services.log_audit(db, "handover_invalidated", "warning", sample_id=sb.id, actor_id=zhao.id,
                               detail={"trigger": "temp_excursion"})
            services.log_audit(db, "review_recorded", "warning", sample_id=sb.id, actor_id=wang.id,
                               detail={"result": "continue", "reason": rv.reason})

        # C：一个待确认交接（演示“确认前位置不变”）
        if created_c:
            db.add(Handover(
                sample_id=sc.id, giver_id=zhang.id, receiver_id=li.id,
                destination="质检室 1 号台", from_location=sc.location,
                status=HandoverStatus.PENDING, sample_version=1, note="待质检",
            ))
            services.log_audit(db, "handover_created", "info", sample_id=sc.id, actor_id=zhang.id,
                               detail={"from": sc.location, "to": "质检室 1 号台"})

        db.commit()
        print("[bootstrap] 初始化完成：表结构、初始人员与演示数据就绪。")


if __name__ == "__main__":
    seed()
