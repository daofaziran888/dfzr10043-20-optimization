"""归档与恢复并发场景的确定性测试。

场景（对应步骤轨迹归档优化需求）：
1. 建两次执行，都含失败的步骤 2，待复核轨迹已选中准备归档；
2. 归档其中一次轨迹的同时，值班员从原轨迹申请恢复；
3. 机器人成功回报重复送达，核对只结案一次；
4. 保存的步骤轨迹仍指向正确的原执行，原失败原因刷新后可查；
5. 原字段回执可重新进入恢复验证而不触发另一场执行；
6. 已结案步骤不再进入恢复集合，归档不会让结案步骤重新派发。
"""
from __future__ import annotations

from app.services import service_for
from app.services.recovery import RECOVERY_CLOSED, RECOVERY_PENDING, recovery_registry


def _create_failed_step2(run_code: str, failure_reason: str, receipt: str) -> tuple[int, int]:
    """建一次执行与其失败的步骤 2，返回 (run_id, trace_id)。"""
    run_svc = service_for("flow_run")
    trace_svc = service_for("step_trace")

    run, missing = run_svc.create_entry({
        "执行编号": run_code,
        "关联模板": "FLOW-TPL-ARCH",
        "执行节点": "ROBO-0002",
        "触发方式": "人工触发",
        "开始时间": "2026-11-24T09:00:00",
        "结束时间": "",
        "步骤进度": "1/2",
        "产出记录数": "0",
    })
    assert not missing

    trace, missing = trace_svc.create_entry({
        "轨迹编号": f"STEP-{run_code}-2",
        "关联执行": run_code,
        "步骤序号": "2",
        "步骤名称": "采集步骤-提交表单",
        "输入快照": receipt,
        "输出快照": '{"status":"failed"}',
        "耗时毫秒": "850",
        "退出码": "1",
        "失败原因": failure_reason,
        "原字段回执": receipt,
    })
    assert not missing
    # 失败步骤进入异常，随后提交复核，成为「待复核且已选中准备归档」的轨迹
    trace_svc.mark_abnormal(trace["id"], exit_code=1, failure_reason=failure_reason)
    trace_svc.run_action(trace["id"], "提交复核")
    return run["id"], trace["id"]


class TestArchiveRecoveryConcurrency:
    def _setup_two_runs(self):
        run_a, trace_a = _create_failed_step2(
            "RUN-ARCH-A", "元素定位超时，表单未提交", '{"form":"order_a","sku":"A-100"}'
        )
        run_b, trace_b = _create_failed_step2(
            "RUN-ARCH-B", "接口限流 429，采集被拒", '{"form":"order_b","sku":"B-200"}'
        )
        return run_a, trace_a, run_b, trace_b

    def test_archive_and_recovery_same_time_keeps_failure_input(self):
        """归档与恢复同时发生（两种先后顺序）都能稳定读取原失败步骤的输入。"""
        trace_svc = service_for("step_trace")
        _, trace_a, _, trace_b = self._setup_two_runs()

        # 顺序一：先归档，后从原轨迹申请恢复
        trace_svc.run_action(trace_a, "归档轨迹")
        recover_a = trace_svc.request_recovery(trace_a)
        assert recover_a["ok"] is True
        assert recover_a["dispatched"] is True

        # 顺序二：先申请恢复，后归档（归档时恢复仍在进行）
        recover_b = trace_svc.request_recovery(trace_b)
        trace_svc.run_action(trace_b, "归档轨迹")
        assert recover_b["dispatched"] is True

        # 归档后刷新执行，仍可查询步骤原来的失败原因与原输入
        reason_a = trace_svc.get_failure_reason(trace_a)
        reason_b = trace_svc.get_failure_reason(trace_b)
        assert reason_a["失败原因"] == "元素定位超时，表单未提交"
        assert reason_b["失败原因"] == "接口限流 429，采集被拒"
        assert reason_a["输入快照"] == '{"form":"order_a","sku":"A-100"}'
        assert reason_b["原字段回执"] == '{"form":"order_b","sku":"B-200"}'

        # 归档快照固化了采集重试所需的全部关键证据
        archived_a = trace_svc.get_entry(trace_a)
        assert archived_a["status"] == "已归档"
        snapshot = archived_a["归档快照"]
        for field in ("关联执行", "步骤序号", "输入快照", "退出码", "失败原因", "原字段回执"):
            assert snapshot.get(field) not in ("", None)
        # 保存的步骤轨迹仍指向正确的原执行
        assert snapshot["关联执行"] == "RUN-ARCH-A"

    def test_duplicate_success_report_closes_only_once(self):
        """成功回报重复送达只结案一次，历史失败与有效恢复记录分别可核对。"""
        trace_svc = service_for("step_trace")
        _, trace_a, _, _ = self._setup_two_runs()

        trace_svc.run_action(trace_a, "归档轨迹")
        trace_svc.request_recovery(trace_a)

        report = {
            "回报编号": "RPT-A-OK-1",
            "退出码": "0",
            "字段回执": '{"form":"order_a","sku":"A-100"}',
            "摘要": "重试采集成功",
        }
        first = trace_svc.submit_retry_report(trace_a, report)
        assert first["closed"] is True
        assert first["duplicate"] is False
        assert first["结案次数"] == 1

        # 机器人迟到的重复成功回报：不二次结案
        duplicate = trace_svc.submit_retry_report(trace_a, {**report, "回报编号": "RPT-A-OK-2"})
        assert duplicate["closed"] is False
        assert duplicate["duplicate"] is True
        assert duplicate["结案次数"] == 1

        trace = trace_svc.get_entry(trace_a)
        assert trace["恢复状态"] == RECOVERY_CLOSED
        assert trace["结案次数"] == 1
        assert trace["结案回报"]["回报编号"] == "RPT-A-OK-1"

        # 历史失败与有效恢复记录分簿登记，可分别核对
        item = recovery_registry.get_item(trace_a)
        failure_events = [h["事件"] for h in item["失败历史"]]
        recovery_events = [h["事件"] for h in item["恢复历史"]]
        assert "原步骤失败" in failure_events
        assert "恢复申请" in recovery_events
        assert "重试成功结案" in recovery_events
        assert "迟到重复回报" in recovery_events

    def test_archived_closed_step_is_not_redispatched(self):
        """归档不得让已经结案的步骤重新派发。"""
        trace_svc = service_for("step_trace")
        _, trace_a, _, _ = self._setup_two_runs()

        trace_svc.request_recovery(trace_a)
        trace_svc.submit_retry_report(trace_a, {"回报编号": "RPT-A1", "退出码": "0"})
        # 结案后再走既有归档入口
        archive_result = trace_svc.run_action(trace_a, "归档轨迹")
        assert archive_result[0] is not None

        # 再次申请恢复：不派发，状态保持已结案
        again = trace_svc.request_recovery(trace_a)
        assert again["dispatched"] is False
        assert again["恢复状态"] == RECOVERY_CLOSED
        assert trace_svc.get_entry(trace_a)["恢复状态"] == RECOVERY_CLOSED

        # 再验证入口同样驳回，不会借验证重新派发
        revalidate = trace_svc.revalidate_original_receipt(trace_a)
        assert revalidate["ok"] is False

    def test_recovery_list_only_contains_still_recovering_steps(self):
        """归档结果同步恢复清单：已完成（结案）步骤不再进入恢复集合。"""
        trace_svc = service_for("step_trace")
        retry_svc = service_for("retry_policy")
        _, trace_a, _, trace_b = self._setup_two_runs()

        # A：归档与恢复同时发生后成功结案；B：归档后仍在恢复
        trace_svc.run_action(trace_a, "归档轨迹")
        trace_svc.request_recovery(trace_a)
        trace_svc.submit_retry_report(trace_a, {"回报编号": "RPT-A", "退出码": "0"})

        trace_svc.request_recovery(trace_b)
        trace_svc.run_action(trace_b, "归档轨迹")

        pending = trace_svc.list_recovery_items()
        pending_ids = {item["trace_id"] for item in pending}
        assert trace_b in pending_ids
        assert trace_a not in pending_ids

        # 重试策略读取同一份恢复清单，按原执行可分组拉取
        grouped = recovery_registry.list_by_execution()
        assert "RUN-ARCH-A" not in grouped
        assert [item["trace_id"] for item in grouped["RUN-ARCH-B"]] == [trace_b]
        assert retry_svc.read_recovery_list("RUN-ARCH-B")[0]["trace_id"] == trace_b

    def test_next_retry_uses_original_receipt_without_new_execution(self):
        """下次重试采用原轨迹字段回执，原回执再验证不触发另一场执行。"""
        trace_svc = service_for("step_trace")
        retry_svc = service_for("retry_policy")
        run_svc = service_for("flow_run")
        _, trace_a, _, trace_b = self._setup_two_runs()

        run_count_before = len(run_svc.list_entries(size=10000)[0])
        policy_id = retry_svc.list_entries(size=1000)[0][0]["id"]

        # 归档与恢复同时发生
        trace_svc.run_action(trace_a, "归档轨迹")
        trace_svc.request_recovery(trace_a)

        next_retry = retry_svc.build_next_retry(trace_a, policy_id=policy_id)
        assert next_retry["ok"] is True
        assert next_retry["dispatched"] is False
        # 回执来自原轨迹（此时取归档快照中固化的原字段回执）
        assert next_retry["原字段回执"] == '{"form":"order_a","sku":"A-100"}'
        assert next_retry["关联执行"] == "RUN-ARCH-A"

        # 未新增任何流程执行实例，步骤仍保持恢复中
        run_count_after = len(run_svc.list_entries(size=10000)[0])
        assert run_count_after == run_count_before
        assert trace_svc.get_entry(trace_a)["恢复状态"] == RECOVERY_PENDING

        # 另一条执行的步骤互不串扰，回执指向各自的原执行
        trace_svc.request_recovery(trace_b)
        next_retry_b = retry_svc.build_next_retry(trace_b)
        assert next_retry_b["原字段回执"] == '{"form":"order_b","sku":"B-200"}'
        assert next_retry_b["关联执行"] == "RUN-ARCH-B"

    def test_failed_report_keeps_step_recovering_and_records_history(self):
        """失败回报不结案，历史失败留痕且步骤继续留在恢复集合。"""
        trace_svc = service_for("step_trace")
        _, trace_a, _, _ = self._setup_two_runs()

        trace_svc.request_recovery(trace_a)
        failed = trace_svc.submit_retry_report(trace_a, {
            "回报编号": "RPT-A-FAIL",
            "退出码": "1",
            "失败原因": "重试仍超时",
        })
        assert failed["closed"] is False
        assert failed["恢复状态"] == RECOVERY_PENDING

        item = recovery_registry.get_item(trace_a)
        assert len(item["失败历史"]) == 2  # 原步骤失败 + 重试失败
        assert any(h["事件"] == "重试失败" for h in item["失败历史"])
        # 仍在恢复清单内等待下次重试
        assert {i["trace_id"] for i in trace_svc.list_recovery_items()} == {trace_a}
