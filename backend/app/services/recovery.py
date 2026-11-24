"""步骤恢复清单：归档与重试之间共享的恢复证据登记处。

恢复清单不单独建表，而是把恢复状态直接挂在步骤轨迹（step_trace）行上：
轨迹行在归档时保留全部采集重试所需的关键数据（输入/输出快照、退出码、
失败原因、原字段回执），恢复登记处只负责这些字段的读写约定与幂等流转。

这样即使归档与恢复同时发生，双方操作的都是同一条轨迹行：
- 归档只做证据快照与状态置「已归档」，不会删掉或改写失败输入；
- 已结案（恢复状态=已结案）的步骤在归档时不会被重新派发；
- 机器人迟到的成功回报按同一结案键去重，只结案一次；
- 历史失败与有效恢复记录分簿登记，可分别核对。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

# 归档时必须保留、供采集重试继续使用的关键字段
RECOVERY_EVIDENCE_FIELDS = [
    "轨迹编号", "关联执行", "步骤序号", "步骤名称",
    "输入快照", "输出快照", "退出码", "失败原因", "原字段回执",
]

# 恢复状态
RECOVERY_PENDING = "恢复中"
RECOVERY_CLOSED = "已结案"


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


class RecoveryRegistry:
    """以步骤轨迹行为载体的恢复清单，提供幂等的恢复/结案/再验证操作。"""

    MODULE = "step_trace"

    # ── 基础读取 ──────────────────────────────────────────────

    def _trace(self, trace_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, trace_id)

    @staticmethod
    def _history(trace: dict[str, Any]) -> list[dict[str, Any]]:
        trace.setdefault("恢复历史", [])
        return trace["恢复历史"]

    @staticmethod
    def _failure_history(trace: dict[str, Any]) -> list[dict[str, Any]]:
        trace.setdefault("失败历史", [])
        return trace["失败历史"]

    def get_item(self, trace_id: int) -> dict[str, Any] | None:
        """读取单条恢复清单项；轨迹不存在或从未申请恢复时返回 None。"""
        trace = self._trace(trace_id)
        if trace is None or trace.get("恢复状态") is None:
            return None
        return self._snapshot_item(trace)

    def list_items(self, execution_code: str | None = None) -> list[dict[str, Any]]:
        """恢复清单：默认只返回仍需恢复（恢复中）的执行与步骤证据。"""
        items: list[dict[str, Any]] = []
        for trace in store.rows(self.MODULE):
            if trace.get("恢复状态") != RECOVERY_PENDING:
                continue
            if execution_code is not None and trace.get("关联执行") != execution_code:
                continue
            items.append(self._snapshot_item(trace))
        return items

    def list_by_execution(self) -> dict[str, list[dict[str, Any]]]:
        """按原执行编号分组的恢复清单，供重试策略逐执行拉取。"""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in self.list_items():
            grouped.setdefault(item["关联执行"], []).append(item)
        return grouped

    def _snapshot_item(self, trace: dict[str, Any]) -> dict[str, Any]:
        return {
            "trace_id": trace["id"],
            "轨迹编号": trace.get("轨迹编号", ""),
            "关联执行": trace.get("关联执行", ""),
            "步骤序号": trace.get("步骤序号", ""),
            "步骤名称": trace.get("步骤名称", ""),
            "恢复状态": trace.get("恢复状态"),
            "失败原因": trace.get("失败原因", ""),
            "原字段回执": trace.get("原字段回执", trace.get("输入快照", "")),
            "恢复次数": int(trace.get("恢复次数", 0) or 0),
            "结案次数": int(trace.get("结案次数", 0) or 0),
            "结案回报": trace.get("结案回报"),
            "失败历史": list(self._failure_history(trace)),
            "恢复历史": list(self._history(trace)),
        }

    # ── 归档协同 ──────────────────────────────────────────────

    def sync_archive(self, trace_id: int) -> dict[str, Any]:
        """归档轨迹时同步恢复清单：固化证据快照，但不改变恢复流转。

        - 未申请恢复的轨迹：仅登记归档快照，不产生派单；
        - 恢复中的轨迹：保持恢复中，证据随归档快照固化，重试可继续读；
        - 已结案的轨迹：保持已结案，归档不会让其重新进入恢复集合。
        """
        trace = self._trace(trace_id)
        if trace is None:
            return {"ok": False, "reason": "轨迹不存在"}
        snapshot = {field: trace.get(field, "") for field in RECOVERY_EVIDENCE_FIELDS}
        snapshot["归档时间"] = _now_iso()
        trace["归档快照"] = snapshot
        if trace.get("恢复状态") == RECOVERY_CLOSED:
            # 已结案步骤归档后仍排除在恢复集合之外，不重新派发
            trace["恢复状态"] = RECOVERY_CLOSED
        self._history(trace).append({
            "事件": "归档同步",
            "时间": _now_iso(),
            "恢复状态": trace.get("恢复状态"),
            "重新派发": False,
        })
        return {
            "ok": True,
            "trace_id": trace_id,
            "恢复状态": trace.get("恢复状态"),
            "归档快照": snapshot,
            "重新派发": False,
        }

    # ── 恢复申请（值班员从原轨迹发起） ─────────────────────────

    def request_recovery(self, trace_id: int, operator: str = "值班员") -> dict[str, Any]:
        """从原轨迹申请恢复，幂等：重复申请不会产生第二场执行。

        返回体含 ``dispatched`` 标记，仅首次申请为 True；
        已结案的步骤拒绝重新派发，只回传既有结案信息。
        """
        trace = self._trace(trace_id)
        if trace is None:
            return {"ok": False, "reason": "轨迹不存在"}

        # 已结案：归档/重复申请均不得重新派发
        if trace.get("恢复状态") == RECOVERY_CLOSED:
            return {
                "ok": True,
                "trace_id": trace_id,
                "dispatched": False,
                "恢复状态": RECOVERY_CLOSED,
                "reason": "步骤已结案，不再派发",
                "结案回报": trace.get("结案回报"),
            }

        first_request = trace.get("恢复状态") is None
        if first_request:
            trace.setdefault("恢复状态", RECOVERY_PENDING)
            trace["原执行标识"] = trace.get("关联执行", "")
            # 下次重试沿用原轨迹的字段回执；未单独沉淀时取原输入快照
            trace.setdefault("原字段回执", trace.get("输入快照", ""))
            trace.setdefault("恢复次数", 0)
            trace.setdefault("结案次数", 0)
            # 首次登记时固化原失败证据，后续刷新仍可核对原失败原因
            self._failure_history(trace).append({
                "事件": "原步骤失败",
                "时间": _now_iso(),
                "失败原因": trace.get("失败原因", ""),
                "退出码": trace.get("退出码", ""),
                "原字段回执": trace.get("原字段回执", ""),
            })
        trace["恢复状态"] = RECOVERY_PENDING
        self._history(trace).append({
            "事件": "恢复申请",
            "时间": _now_iso(),
            "操作者": operator,
            "首次派发": first_request,
        })
        return {
            "ok": True,
            "trace_id": trace_id,
            "dispatched": first_request,
            "恢复状态": RECOVERY_PENDING,
            "关联执行": trace.get("关联执行", ""),
            "原字段回执": trace.get("原字段回执", ""),
        }

    # ── 机器人重试回报（可能迟到、可能重复） ──────────────────

    def submit_report(self, trace_id: int, report: dict[str, Any]) -> dict[str, Any]:
        """提交机器人重试回报，按结案键幂等：成功回报只结案一次。

        - 成功（退出码 0）：首次到达结案并归档结案回报，重复/迟到到达只认重复；
        - 失败（退出码非 0）：登记一次失败历史，保持恢复中等待下次重试；
        - 未申请恢复却收到回报：不自动派单，仅登记后提示。
        """
        trace = self._trace(trace_id)
        if trace is None:
            return {"ok": False, "reason": "轨迹不存在"}

        exit_code = str(report.get("退出码", trace.get("退出码", "1")))
        success = exit_code == "0"
        receipt = report.get("字段回执", trace.get("原字段回执", trace.get("输入快照", "")))
        payload = {
            "回报编号": report.get("回报编号", f"RPT-{trace_id}-{_now_iso()}"),
            "退出码": exit_code,
            "字段回执": receipt,
            "回报时间": report.get("回报时间", _now_iso()),
            "摘要": report.get("摘要", ""),
        }

        if success and trace.get("恢复状态") == RECOVERY_CLOSED:
            # 迟到的重复成功回报：只认重复，不二次结案、不二次派发
            self._history(trace).append({
                "事件": "迟到重复回报",
                "时间": _now_iso(),
                "回报编号": payload["回报编号"],
            })
            return {
                "ok": True,
                "trace_id": trace_id,
                "closed": False,
                "duplicate": True,
                "结案次数": int(trace.get("结案次数", 0) or 0),
                "reason": "成功回报重复送达，步骤此前已结案",
            }

        if success:
            trace["恢复状态"] = RECOVERY_CLOSED
            trace["结案次数"] = int(trace.get("结案次数", 0) or 0) + 1
            trace["结案回报"] = payload
            trace["退出码"] = "0"
            self._history(trace).append({
                "事件": "重试成功结案",
                "时间": _now_iso(),
                "回报编号": payload["回报编号"],
            })
            return {
                "ok": True,
                "trace_id": trace_id,
                "closed": True,
                "duplicate": False,
                "结案次数": trace["结案次数"],
                "结案回报": payload,
            }

        # 失败回报：沉淀历史失败，保持恢复中
        self._failure_history(trace).append({
            "事件": "重试失败",
            "时间": _now_iso(),
            "失败原因": report.get("失败原因", trace.get("失败原因", "")),
            "退出码": exit_code,
            "回报编号": payload["回报编号"],
        })
        self._history(trace).append({
            "事件": "重试失败回报",
            "时间": _now_iso(),
            "回报编号": payload["回报编号"],
        })
        if trace.get("恢复状态") is None:
            trace["恢复状态"] = RECOVERY_PENDING
            trace.setdefault("原字段回执", trace.get("输入快照", ""))
        return {
            "ok": True,
            "trace_id": trace_id,
            "closed": False,
            "duplicate": False,
            "恢复状态": trace.get("恢复状态"),
        }

    # ── 原字段回执再验证（不触发新执行） ─────────────────────

    def revalidate_with_original_receipt(self, trace_id: int) -> dict[str, Any]:
        """用原轨迹沉淀的字段回执重新进入恢复验证，不产生新的执行派单。

        仅恢复中的步骤可再验证；已结案步骤直接驳回，避免借再验证重新派发。
        """
        trace = self._trace(trace_id)
        if trace is None:
            return {"ok": False, "reason": "轨迹不存在"}
        if trace.get("恢复状态") == RECOVERY_CLOSED:
            return {
                "ok": False,
                "trace_id": trace_id,
                "reason": "步骤已结案，原字段回执不再进入恢复验证",
                "恢复状态": RECOVERY_CLOSED,
            }

        receipt = trace.get("原字段回执", trace.get("输入快照", ""))
        trace["恢复次数"] = int(trace.get("恢复次数", 0) or 0) + 1
        self._history(trace).append({
            "事件": "原回执再验证",
            "时间": _now_iso(),
            "字段回执": receipt,
        })
        return {
            "ok": True,
            "trace_id": trace_id,
            "dispatched": False,
            "恢复状态": trace.get("恢复状态", RECOVERY_PENDING),
            "关联执行": trace.get("关联执行", ""),
            "原字段回执": receipt,
            "恢复次数": trace["恢复次数"],
        }


# 模块级单例：服务层与流水线共用同一份恢复清单
recovery_registry = RecoveryRegistry()
