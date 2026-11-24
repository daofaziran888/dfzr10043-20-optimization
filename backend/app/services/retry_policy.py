"""重试策略服务：退避参数、放弃阈值与重试执行管理。

维护异常处置对应的重试策略，含退避基数、倍数和抖动区间，
达到放弃阈值后抑制并为下游写入审计轨迹。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.recovery import RECOVERY_PENDING, recovery_registry


class RetryPolicyService(BaseModuleService):
    MODULE = "retry_policy"
    ENTITY = "重试策略"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "策略编号", "关联规则", "最大次数", "退避基数", "退避倍数",
        "抖动区间", "放弃阈值", "累计重试",
    ]
    REQUIRED_FIELDS = ["策略编号", "关联规则", "最大次数"]
    STATUS_ORDER = ["草稿", "执行中", "已放弃", "已停用"]
    ACTION_RULES = {"启用策略": "执行中", "标记放弃": "已放弃", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def should_abandon(self, policy_id: int) -> bool:
        """判断重试是否应放弃：累计重试>=最大次数或达到放弃阈值。"""
        entry = self.get_entry(policy_id)
        if entry is None:
            return True
        max_attempts = int(entry.get("最大次数", "3") or "3")
        current = int(entry.get("累计重试", "0") or "0")
        threshold = int(entry.get("放弃阈值", str(max_attempts)) or str(max_attempts))
        return current >= threshold or current >= max_attempts

    def calculate_backoff(self, policy_id: int, attempt: int) -> int:
        """计算退避等待毫秒数，含指数退避与抖动。"""
        entry = self.get_entry(policy_id)
        if entry is None:
            return 1000
        base = int(entry.get("退避基数", "500") or "500")
        multiplier = int(entry.get("退避倍数", "2") or "2")
        jitter_range = entry.get("抖动区间", "200") or "200"
        jitter = int(jitter_range)
        import random
        return base * (multiplier ** (attempt - 1)) + random.randint(0, jitter)

    def increment_retry(self, policy_id: int) -> dict[str, Any]:
        """递增累计重试次数。"""
        entry = self.get_entry(policy_id)
        if entry is None:
            return {"ok": False, "reason": "策略不存在"}
        current = int(entry.get("累计重试", "0") or "0")
        entry["累计重试"] = str(current + 1)
        return {"ok": True, "policy_id": policy_id, "total_retries": current + 1}

    # ── 恢复清单驱动的重试 ────────────────────────────────────

    def read_recovery_list(self, execution_code: str | None = None) -> list[dict[str, Any]]:
        """读取恢复清单：只含仍需恢复（恢复中）的执行与步骤证据。

        已结案/已完成的步骤不在集合内，归档不改变该筛选结果。
        """
        return recovery_registry.list_items(execution_code=execution_code)

    def build_next_retry(self, trace_id: int, policy_id: int | None = None) -> dict[str, Any]:
        """为恢复中的步骤构建下次重试，沿用原轨迹的字段回执。

        关键约束：
        - 已结案步骤返回 ``dispatched=False``，不再进入重试集合；
        - 重试载荷的字段回执取自原轨迹（归档后取归档快照），不新造输入；
        - 构建动作本身不派发新的流程执行，只递增恢复验证计数。
        """
        item = recovery_registry.get_item(trace_id)
        if item is None:
            return {"ok": False, "reason": "该步骤未申请恢复，无需重试"}
        if item["恢复状态"] != RECOVERY_PENDING:
            return {
                "ok": True,
                "trace_id": trace_id,
                "dispatched": False,
                "恢复状态": item["恢复状态"],
                "reason": "步骤已结案，不再重试",
            }

        backoff_ms = self.calculate_backoff(policy_id, item["恢复次数"] + 1) if policy_id else 0
        # 用原字段回执再验证，登记一次恢复但不产生另一场执行
        validation = recovery_registry.revalidate_with_original_receipt(trace_id)
        if policy_id is not None and self.get_entry(policy_id) is not None:
            self.increment_retry(policy_id)
        return {
            "ok": True,
            "trace_id": trace_id,
            "dispatched": False,
            "关联执行": item["关联执行"],
            "步骤序号": item["步骤序号"],
            "恢复状态": RECOVERY_PENDING,
            "原字段回执": item["原字段回执"],
            "失败原因": item["失败原因"],
            "退避毫秒": backoff_ms,
            "恢复次数": validation.get("恢复次数"),
        }