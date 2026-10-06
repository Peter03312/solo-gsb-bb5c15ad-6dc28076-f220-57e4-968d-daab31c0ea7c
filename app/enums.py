import enum


class SampleStatus(str, enum.Enum):
    NORMAL = "normal"            # 在库/正常流转
    ISOLATED = "isolated"        # 隔离中，禁止转移


class HandoverStatus(str, enum.Enum):
    PENDING = "pending"          # 已提交，待接收人确认（位置不变）
    CONFIRMED = "confirmed"      # 接收人已确认，位置更新为目的地
    INVALIDATED = "invalidated"  # 越界隔离导致待确认交接失效
    CANCELLED = "cancelled"      # 交出人/工作人员取消


class ReviewResult(str, enum.Enum):
    RELEASE = "release"                    # 复核通过，解除隔离
    CONTINUE_ISOLATION = "continue"        # 继续隔离


STATUS_LABELS = {
    SampleStatus.NORMAL: "正常",
    SampleStatus.ISOLATED: "隔离中",
    HandoverStatus.PENDING: "待确认",
    HandoverStatus.CONFIRMED: "已确认",
    HandoverStatus.INVALIDATED: "已失效",
    HandoverStatus.CANCELLED: "已取消",
    ReviewResult.RELEASE: "复核通过，解除隔离",
    ReviewResult.CONTINUE_ISOLATION: "复核结论：继续隔离",
}

# 审计动作 -> 中文
ACTION_LABELS = {
    "sample_created": "样品录入",
    "staff_created": "人员建档",
    "handover_created": "发起交接",
    "handover_confirmed": "交接确认",
    "handover_cancelled": "交接取消",
    "handover_invalidated": "交接失效",
    "reading_recorded": "温度记录",
    "reading_corrected": "读数更正",
    "temp_excursion": "温度越界",
    "isolation_opened": "隔离开启",
    "isolation_closed": "隔离解除",
    "review_recorded": "复核结论",
    "transfer_rejected": "拒绝转移",
    "confirm_rejected": "确认被拒",
    "state_conflict": "并发冲突",
}

# 审计类别 -> 中文 / 颜色等级
CATEGORY_LABELS = {
    "info": "常规",
    "warning": "关注",
    "exception": "异常",
    "conflict": "冲突",
}
