"""端到端业务测试：对真实 PostgreSQL 运行。

覆盖：
1. 录入 → 发起交接 → 确认前位置不变 → 确认后位置变更
2. 越界读数 → 立即隔离 + 待确认交接失效
3. 隔离期间拒绝转移（发起/确认）
4. 复核：继续隔离 / 只有注明理由的通过可解除
5. 更正读数保留原值/理由/操作者，且不自动解除隔离
6. 并发确认 / 并发越界隔离：仅一个生效，冲突明确提示
7. 版本号过期操作被拒绝
8. 审计导出完整
"""

import os
import threading
import traceback

from fastapi.testclient import TestClient

# 默认走测试环境注入的 DATABASE_URL
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://lab:labpass@/labflow?host=/tmp/pgdata"
)

from app.database import engine, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    Handover,
    Isolation,
    Review,
    Sample,
    TemperatureReading,
)
from sqlalchemy import text  # noqa: E402

client = TestClient(app)
PASS = 0


def check(name: str, cond: bool, detail=""):
    global PASS
    assert cond, f"❌ {name} {detail}"
    PASS += 1
    print(f"  ✓ {name}")


def reset_db():
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    from app.bootstrap import seed

    seed()


def staff_ids():
    r = client.get("/api/staff")
    r.raise_for_status()
    return {s["name"]: s["id"] for s in r.json()}


def create_sample(sample_no, lo, hi, loc, creator):
    r = client.post("/api/samples", json={
        "sample_no": sample_no, "name": "测试样", "temp_min": lo, "temp_max": hi,
        "location": loc, "created_by_id": creator,
    })
    r.raise_for_status()
    return r.json()


def main():
    reset_db()
    s = staff_ids()
    zhang, li, wang, zhao = s["张工"], s["李工"], s["王工"], s["赵工"]

    print("\n[1] 交接：确认前位置不变，确认后变更")
    smp = create_sample("T-001", 2, 8, "冷库A", zhang)
    r = client.post(f"/api/samples/{smp['id']}/handovers", json={
        "giver_id": zhang, "receiver_id": li, "destination": "质检室", "note": "送检",
    })
    r.raise_for_status()
    h = r.json()
    s_now = client.get(f"/api/samples/{smp['id']}").json()
    check("确认前位置保持不变", s_now["location"] == "冷库A", s_now["location"])

    # 非接收人不能确认
    r = client.post(f"/api/handovers/{h['id']}/confirm?operator_id={zhang}")
    check("非接收人确认被拒绝(403)", r.status_code == 403, r.text[:200])

    r = client.post(f"/api/handovers/{h['id']}/confirm?operator_id={li}")
    r.raise_for_status()
    s_now = client.get(f"/api/samples/{smp['id']}").json()
    check("确认后位置更新为目的地", s_now["location"] == "质检室", s_now["location"])
    check("确认后版本号递增", s_now["version"] == 2, str(s_now["version"]))

    # 重复确认冲突
    r = client.post(f"/api/handovers/{h['id']}/confirm?operator_id={li}")
    check("重复确认返回 409", r.status_code == 409 and "已确认" in r.json()["error"]["message"])

    print("\n[2] 越界读数：立即隔离 + 待确认交接失效")
    smp2 = create_sample("T-002", 2, 8, "冷库B", zhang)
    h2 = client.post(f"/api/samples/{smp2['id']}/handovers", json={
        "giver_id": zhang, "receiver_id": li, "destination": "实验室",
    }).json()
    r = client.post(f"/api/samples/{smp2['id']}/readings", json={
        "value": 25.0, "recorder_id": zhao, "expected_version": smp2["version"],
    })
    check("越界读数 201", r.status_code == 201, r.text[:200])
    body = r.json()
    check("响应告知已隔离", body["isolation_opened"] is True)
    check("响应告知交接失效", body["invalidated_handover_ids"] == [h2["id"]])
    s2 = client.get(f"/api/samples/{smp2['id']}").json()
    check("样品状态变为隔离", s2["status"] == "isolated", s2["status"])
    h2_now = client.get(f"/api/handovers/{h2['id']}").json()
    check("待确认交接已失效", h2_now["status"] == "invalidated", h2_now["status"])
    check("失效后位置未改变", s2["location"] == "冷库B", s2["location"])

    # 再次越界：已隔离样品不重复开启隔离，但记录异常
    r = client.post(f"/api/samples/{smp2['id']}/readings", json={
        "value": 30.0, "recorder_id": zhao, "expected_version": s2["version"],
    })
    check("已隔离样品再次越界仍记录 201", r.status_code == 201)
    check("不重复开启隔离", r.json()["isolation_opened"] is False)
    with SessionLocal() as db:
        open_iso = db.query(Isolation).filter(
            Isolation.sample_id == smp2["id"], Isolation.released_at.is_(None)
        ).count()
    check("仅一条未解除隔离", open_iso == 1, str(open_iso))

    print("\n[3] 隔离期间拒绝转移")
    r = client.post(f"/api/samples/{smp2['id']}/handovers", json={
        "giver_id": zhang, "receiver_id": li, "destination": "外地",
    })
    check("隔离中发起交接 409", r.status_code == 409 and "隔离" in r.json()["error"]["message"])

    # 构造新的 pending 交接再越界，然后尝试确认 -> 409 且交接失效
    smp3 = create_sample("T-003", 2, 8, "冷库C", zhang)
    h3 = client.post(f"/api/samples/{smp3['id']}/handovers", json={
        "giver_id": zhang, "receiver_id": li, "destination": "X 室",
    }).json()
    s3 = client.get(f"/api/samples/{smp3['id']}").json()
    client.post(f"/api/samples/{smp3['id']}/readings", json={
        "value": 0.5, "recorder_id": zhao, "expected_version": s3["version"],
    })
    r = client.post(f"/api/handovers/{h3['id']}/confirm?operator_id={li}")
    check("隔离后确认旧交接 409", r.status_code == 409 and "隔离" in r.json()["error"]["message"])
    check("旧交接标记失效", client.get(f"/api/handovers/{h3['id']}").json()["status"] == "invalidated")
    s3now = client.get(f"/api/samples/{smp3['id']}").json()
    check("确认被拒位置不变", s3now["location"] == "冷库C")

    print("\n[4] 复核解除隔离")
    # 未填理由 -> 422
    r = client.post(f"/api/samples/{smp2['id']}/reviews", json={
        "reviewer_id": wang, "result": "release", "reason": "", "expected_version": 2,
    })
    check("无理由复核被校验拒绝 422", r.status_code == 422, r.text[:120])
    # 继续隔离
    r = client.post(f"/api/samples/{smp2['id']}/reviews", json={
        "reviewer_id": wang, "result": "continue", "reason": "设备维修中，继续观察", "expected_version": 2,
    })
    r.raise_for_status()
    s2 = client.get(f"/api/samples/{smp2['id']}").json()
    check("继续隔离后仍为隔离态", s2["status"] == "isolated")
    # 版本过期
    r = client.post(f"/api/samples/{smp2['id']}/reviews", json={
        "reviewer_id": wang, "result": "release", "reason": "复测合格", "expected_version": 2,
    })
    check("过期版本复核被拒 409", r.status_code == 409 and "过期" in r.json()["error"]["message"], r.text[:160])
    # 通过解除
    s2 = client.get(f"/api/samples/{smp2['id']}").json()
    r = client.post(f"/api/samples/{smp2['id']}/reviews", json={
        "reviewer_id": wang, "result": "release",
        "reason": "传感器故障已排除，独立复测 5.1℃ 合格，准予解除",
        "expected_version": s2["version"],
    })
    r.raise_for_status()
    s2 = client.get(f"/api/samples/{smp2['id']}").json()
    check("复核通过后解除隔离", s2["status"] == "normal")
    # 未隔离再复核 -> 409
    r = client.post(f"/api/samples/{smp2['id']}/reviews", json={
        "reviewer_id": wang, "result": "continue", "reason": "再次复核",
    })
    check("未隔离不能复核 409", r.status_code == 409)

    print("\n[5] 更正读数：保留原值/理由/操作者，不自动解除隔离")
    smp4 = create_sample("T-004", 2, 8, "冷库D", zhang)
    bad_reading = client.post(f"/api/samples/{smp4['id']}/readings", json={
        "value": 99.0, "recorder_id": zhao,
    }).json()
    check("越界后隔离", client.get(f"/api/samples/{smp4['id']}").json()["status"] == "isolated")
    r = client.post(f"/api/readings/{bad_reading['id']}/correct", json={
        "new_value": 5.0, "reason": "探头未入冷柜，测得的是环境温度", "operator_id": zhao,
    })
    check("更正 201", r.status_code == 201, r.text[:200])
    corr = r.json()
    check("更正记录关联原始读数", corr["corrects_reading_id"] == bad_reading["id"])
    check("更正记录保留理由", "探头" in corr["correction_reason"])
    check("更正记录记录操作者", corr["correction_operator_id"] == zhao)
    readings = client.get(f"/api/samples/{smp4['id']}/readings").json()
    original = next(x for x in readings if x["id"] == bad_reading["id"])
    check("原始读数原值保留", float(original["value"]) == 99.0 and original["in_range"] is False)
    s4 = client.get(f"/api/samples/{smp4['id']}").json()
    check("更正不自动解除隔离", s4["status"] == "isolated", s4["status"])
    # 不能对更正记录再次更正
    r = client.post(f"/api/readings/{corr['id']}/correct", json={
        "new_value": 6.0, "reason": "x", "operator_id": zhao,
    })
    check("不能更正“更正记录” 409", r.status_code == 409)
    # 更正缺理由
    r = client.post(f"/api/readings/{bad_reading['id']}/correct", json={
        "new_value": 6.0, "reason": "", "operator_id": zhao,
    })
    check("更正无理由 422", r.status_code == 422)

    print("\n[6] 并发确认：只有一个生效")
    smp5 = create_sample("T-005", 2, 8, "冷库E", zhang)
    h5 = client.post(f"/api/samples/{smp5['id']}/handovers", json={
        "giver_id": zhang, "receiver_id": li, "destination": "并发目的地",
    }).json()
    results = []

    def confirm():
        with TestClient(app) as c2:
            r = c2.post(f"/api/handovers/{h5['id']}/confirm?operator_id={li}")
            results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    codes = sorted(x[0] for x in results)
    check("两个并发确认一个成功一个冲突", codes == [200, 409], str(codes))
    msg = next(x[1]["error"]["message"] for x in results if x[0] == 409)
    check("冲突信息明确", "已确认" in msg, msg)
    s5 = client.get(f"/api/samples/{smp5['id']}").json()
    check("位置只变更一次", s5["location"] == "并发目的地")

    print("\n[7] 并发越界隔离：只开启一条隔离")
    smp6 = create_sample("T-006", 2, 8, "冷库F", zhang)
    h6 = client.post(f"/api/samples/{smp6['id']}/handovers", json={
        "giver_id": zhang, "receiver_id": li, "destination": "Z",
    }).json()
    iso_results = []

    def record_bad(v):
        with TestClient(app) as c2:
            r = c2.post(f"/api/samples/{smp6['id']}/readings",
                        json={"value": v, "recorder_id": zhao})
            iso_results.append((r.status_code, r.json()))

    ts = [threading.Thread(target=record_bad, args=(v,)) for v in (50.0, 51.0)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("两个并发越界读数均被记录", sorted(x[0] for x in iso_results) == [201, 201])
    opened = [x for x in iso_results if x[1].get("isolation_opened")]
    check("仅一个事务开启隔离", len(opened) == 1, str([x[1].get("isolation_opened") for x in iso_results]))
    with SessionLocal() as db:
        n_open = db.query(Isolation).filter(
            Isolation.sample_id == smp6["id"], Isolation.released_at.is_(None)
        ).count()
        n_inv = db.query(Handover).filter(
            Handover.id == h6["id"], Handover.status == "invalidated"
        ).count()
    check("数据库中仅一条未解除隔离", n_open == 1, str(n_open))
    check("待确认交接已失效（仅一次）", n_inv == 1)
    s6 = client.get(f"/api/samples/{smp6['id']}").json()
    check("样品隔离且位置不变", s6["status"] == "isolated" and s6["location"] == "冷库F")

    print("\n[8] 并发发起交接：仅一个待确认")
    smp7 = create_sample("T-007", 2, 8, "冷库G", zhang)
    create_results = []

    def create_h(dest):
        with TestClient(app) as c2:
            r = c2.post(f"/api/samples/{smp7['id']}/handovers", json={
                "giver_id": zhang, "receiver_id": li, "destination": dest,
            })
            create_results.append((r.status_code, r.text[:120]))

    ts = [threading.Thread(target=create_h, args=(d,)) for d in ("P", "Q")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("并发发起交接一个成功一个冲突", sorted(x[0] for x in create_results) == [201, 409],
          str(create_results))

    print("\n[9] 审计与导出")
    r = client.get("/api/audit?limit=5000")
    audits = r.json()
    actions = {a["action"] for a in audits}
    for required in ["sample_created", "handover_created", "handover_confirmed",
                     "temp_excursion", "isolation_opened", "handover_invalidated",
                     "reading_recorded", "reading_corrected", "review_recorded",
                     "isolation_closed", "confirm_rejected"]:
        check(f"审计包含 {required}", required in actions, str(sorted(actions)))
    r = client.get("/api/audit/export?fmt=csv")
    check("CSV 导出 200 且有表头", r.status_code == 200 and "时间(UTC)" in r.text)
    r = client.get("/api/audit/export?fmt=json")
    check("JSON 导出 200 且为列表", r.status_code == 200 and isinstance(r.json(), list))

    print("\n[10] 页面渲染")
    for path in ("/", f"/samples/{smp['id']}", "/audit"):
        r = client.get(path)
        check(f"页面 {path} 200", r.status_code == 200, r.text[:100])
    r = client.get("/samples/999999")
    check("未知样品 404", r.status_code == 404)

    print(f"\n🎉 全部 {PASS} 项检查通过。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
