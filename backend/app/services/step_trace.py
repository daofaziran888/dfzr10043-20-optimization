"""步骤轨迹服务：执行步骤记录、快照对比、异常标记与归档/恢复协同。

记录流程每一步的输入输出快照与耗时，为失败定位和数据回溯
提供逐步骤证据链。支持复核、异常标记和归档流转。

归档与恢复协同约定（与 app.services.recovery 恢复清单共享）：
- 归档不移动、不删除轨迹行，只固化「归档快照」，原失败步骤的输入、
  失败原因和原字段回执在刷新后仍可查询；
- 归档动作通过恢复清单同步恢复状态，已结案步骤不会被重新派发；
- 恢复申请、重试回报、原回执再验证均为幂等操作，重复成功回报只结案一次。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.recovery import recovery_registry


class StepTraceService(BaseModuleService):
    MODULE = "step_trace"
    ENTITY = "步骤轨迹"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "轨迹编号", "关联执行", "步骤序号", "步骤名称", "输入快照",
        "输出快照", "耗时毫秒", "退出码", "失败原因", "原字段回执",
    ]
    REQUIRED_FIELDS = ["轨迹编号", "关联执行", "步骤序号"]
    STATUS_ORDER = ["已记录", "待复核", "异常", "已归档"]
    ACTION_RULES = {"提交复核": "待复核", "标记异常": "异常", "归档轨迹": "已归档"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记轨迹时同步沉淀失败原因与原字段回执，供后续采集重试复用。"""
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        # 原字段回执缺省取输入快照，保证重试始终能拿到原轨迹字段
        entry["失败原因"] = values.get("失败原因", "") or ""
        entry["原字段回执"] = values.get("原字段回执", "") or entry.get("输入快照", "")
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "归档轨迹":
            # 沿用既有「归档轨迹」入口，归档结果同步到恢复清单
            recovery_registry.sync_archive(entry_id)
        return entry, message

    def mark_abnormal(
        self, trace_id: int, exit_code: int = 1, failure_reason: str = ""
    ) -> dict[str, Any]:
        """标记步骤异常并设置退出码，同时沉淀可追溯的失败原因。"""
        entry = self.get_entry(trace_id)
        if entry is None:
            return {"ok": False, "reason": "轨迹不存在"}
        entry["退出码"] = str(exit_code)
        if failure_reason:
            entry["失败原因"] = failure_reason
        elif not entry.get("失败原因"):
            entry["失败原因"] = f"步骤退出码非零（{exit_code}）"
        self.run_action(trace_id, "标记异常")
        return {
            "ok": True,
            "trace_id": trace_id,
            "exit_code": exit_code,
            "失败原因": entry["失败原因"],
        }

    def get_traces_by_run(self, run_code: str) -> list[dict[str, Any]]:
        """按关联执行编号获取所有步骤轨迹。"""
        items, _ = self.list_entries(size=10000)
        return [it for it in items if it.get("关联执行") == run_code]

    def get_failure_reason(self, trace_id: int) -> dict[str, Any]:
        """查询步骤原来的失败原因；归档后从归档快照读取，保证刷新后仍可核对。"""
        entry = self.get_entry(trace_id)
        if entry is None:
            return {"ok": False, "reason": "轨迹不存在"}
        snapshot = entry.get("归档快照") or {}
        return {
            "ok": True,
            "trace_id": trace_id,
            "status": entry.get("status"),
            "失败原因": entry.get("失败原因", "") or snapshot.get("失败原因", ""),
            "退出码": entry.get("退出码", "") or snapshot.get("退出码", ""),
            "输入快照": entry.get("输入快照", "") or snapshot.get("输入快照", ""),
            "原字段回执": entry.get("原字段回执", "") or snapshot.get("原字段回执", ""),
        }

    def archive_completed_traces(self) -> dict[str, Any]:
        """归档所有已完成步骤的轨迹（沿用既有入口，逐条同步恢复清单）。"""
        items, _ = self.list_entries(size=10000)
        archived = 0
        for it in items:
            if it.get("status") in ("已记录", "待复核"):
                self.run_action(it["id"], "归档轨迹")
                archived += 1
        return {"ok": True, "archived_count": archived}

    # ── 恢复清单协同（幂等） ──────────────────────────────────

    def request_recovery(self, trace_id: int, operator: str = "值班员") -> dict[str, Any]:
        """值班员从原轨迹申请恢复；重复申请不产生第二场执行。"""
        return recovery_registry.request_recovery(trace_id, operator=operator)

    def submit_retry_report(self, trace_id: int, report: dict[str, Any]) -> dict[str, Any]:
        """机器人重试回报；迟到的成功回报按幂等键只结案一次。"""
        return recovery_registry.submit_report(trace_id, report)

    def revalidate_original_receipt(self, trace_id: int) -> dict[str, Any]:
        """原字段回执重新进入恢复验证，不触发另一场执行。"""
        return recovery_registry.revalidate_with_original_receipt(trace_id)

    def list_recovery_items(self, execution_code: str | None = None) -> list[dict[str, Any]]:
        """恢复清单：仅仍需恢复的执行与步骤证据，已完成/已结案步骤不在集合内。"""
        return recovery_registry.list_items(execution_code=execution_code)
