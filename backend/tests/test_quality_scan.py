"""质量扫描分母修复的端到端测试。

复现值班场景：
1. 完整性规则实测 5000 行命中 12 条异常（0.24%），另一条规则只有缺分母的旧摘要；
2. 核对实测异常率、评分与人工复核门槛（低于 80 分）；
3. 按门槛查询可转交订单并核对覆盖范围（仅订单级，不是整批）；
4. 同批同规则重试只保留一个有效结果，异常数/分母不重复累加；
5. 补齐另一规则分母后重新扫描，核对新分数，并验证历史转交记录仍保留原依据。
"""
from __future__ import annotations

from app.fixtures import QUALITY_RESCAN_SAMPLES
from app.services.pipeline_orchestrator import (
    list_pipeline_results,
    run_quality_rescan,
    run_quality_scan,
)
from app.services.quality_scan import (
    MEASURED_TABLE,
    REVIEW_SCORE_THRESHOLD,
    TRANSFER_TABLE,
    quality_scan_service,
)
from app.store import store


class TestMeasuredDenominator:
    """实测分母取代 1000 行占位值。"""

    def test_scan_uses_actual_scanned_rows(self):
        result = run_quality_scan()
        # 只有完整性规则有实测分母：5000 行 / 12 条异常。
        assert result["total_scanned_rows"] == 5000
        assert result["total_anomalies"] == 12
        # 12 / 5000 = 0.24%
        assert result["anomaly_rate_percent"] == "0.24%"
        assert abs(result["anomaly_rate"] - 0.0024) < 1e-9

    def test_placeholder_denominator_no_longer_used(self):
        result = run_quality_scan()
        # 旧实现会把分母估成 max(12, 1000)=1000，这里必须等于实测 5000。
        assert result["total_rows_estimated"] == 5000
        rule = next(r for r in result["rule_results"] if r["规则编号"] == "QUAL-COMPLETENESS")
        assert rule["扫描行数"] == 5000
        assert rule["异常行数"] == 12
        assert rule["异常率百分比"] == "0.24%"
        assert rule["实测范围"]  # 局部覆盖必须能核对实测范围

    def test_score_below_threshold_triggers_review(self):
        result = run_quality_scan()
        # 评分 = 100 - 0.0024*10000 = 76，低于 80 分门槛。
        assert result["quality_score"] == 76
        assert result["quality_score"] < REVIEW_SCORE_THRESHOLD
        assert result["needs_manual_review"] is True

    def test_missing_denominator_summary_not_used_as_denominator(self):
        run_quality_scan()
        summary = quality_scan_service.summarize()
        # 准确性规则只有 3 条异常的历史摘要，分母缺失。
        missing = {item["规则编号"] for item in summary["缺分母摘要"]}
        assert "QUAL-ACCURACY" in missing
        # 缺分母的 3 条异常既不进总异常数，也不进总分母。
        assert summary["总异常行数"] == 12
        assert summary["总扫描行数"] == 5000
        # 实测结果与缺分母摘要分表存储、状态可辨。
        rows = quality_scan_service.list_measured_results()
        accuracy = next(r for r in rows if r["规则编号"] == "QUAL-ACCURACY")
        assert accuracy["扫描行数"] is None
        assert accuracy["数据状态"] == "缺分母"


class TestIdempotentUpsert:
    """同批同规则重试只能留下一个有效结果。"""

    def test_retry_same_batch_rule_overrides(self):
        run_quality_scan()
        before = len(store.rows(MEASURED_TABLE))
        # 用同样的批次×规则重报一遍（模拟重试）。
        payload = {
            "批次编号": "BATCH-20260830-01",
            "规则编号": "QUAL-COMPLETENESS",
            "监控对象": "order_events",
            "校验类型": "完整性",
            "扫描行数": 5000,
            "异常行数": 12,
            "实测范围": "order_events 2026-08-30 第01批(行1-5000)",
            "异常订单": [f"ORD-20260830-{idx:04d}" for idx in range(1, 13)],
        }
        entry, missing = quality_scan_service.upsert_measured_result(payload)
        assert missing == []
        assert entry is not None
        after = len(store.rows(MEASURED_TABLE))
        assert after == before  # 没有新增行
        # 汇总值不翻倍。
        summary = quality_scan_service.summarize()
        assert summary["总扫描行数"] == 5000
        assert summary["总异常行数"] == 12

    def test_retry_with_corrected_counts_takes_effect_once(self):
        run_quality_scan()
        payload = {
            "批次编号": "BATCH-20260830-01",
            "规则编号": "QUAL-COMPLETENESS",
            "监控对象": "order_events",
            "扫描行数": 5000,
            "异常行数": 10,
            "实测范围": "order_events 2026-08-30 第01批(行1-5000)复核版",
        }
        quality_scan_service.upsert_measured_result(payload)
        rows = [r for r in quality_scan_service.list_measured_results()
                if r["规则编号"] == "QUAL-COMPLETENESS"]
        assert len(rows) == 1
        assert rows[0]["异常行数"] == 10
        assert rows[0]["实测范围"].endswith("复核版")

    def test_invalid_denominator_rejected(self):
        bad = {"批次编号": "B1", "规则编号": "R1", "监控对象": "x", "异常行数": 5, "扫描行数": 0}
        _, missing = quality_scan_service.upsert_measured_result(bad)
        assert "扫描行数" in missing
        bad2 = {"批次编号": "B1", "规则编号": "R1", "监控对象": "x", "异常行数": 5, "扫描行数": 3}
        _, missing2 = quality_scan_service.upsert_measured_result(bad2)
        assert "异常行数" in missing2


class TestDailyReportAndTransfer:
    """交班日报、人工复核门槛与转交溯源。"""

    def test_daily_report_distinguishes_order_scope_from_batch_scope(self):
        run_quality_scan()
        report = quality_scan_service.build_daily_report()
        assert report["质量评分"] == 76
        assert report["需人工复核"] is True
        assert "QUAL-ACCURACY" in report["缺分母规则"]
        # 处理意见只覆盖 12 笔订单，明确不是整批结论。
        assert report["可处理异常订单数"] == 12
        assert "仅覆盖" in report["处理意见"]
        assert "整批" in report["处理意见"]

    def test_transferable_orders_have_traceable_evidence(self):
        run_quality_scan()
        orders = quality_scan_service.list_transferable_orders()
        assert len(orders) == 12
        first = orders[0]
        assert first["扫描行数"] == 5000
        assert first["异常行数"] == 12
        assert first["规则编号"] == "QUAL-COMPLETENESS"
        assert first["批次编号"] == "BATCH-20260830-01"
        assert "5000" in first["依据"] and "12" in first["依据"]

    def test_transfer_records_snapshot_original_evidence(self):
        run_quality_scan()
        orders = quality_scan_service.list_transferable_orders()
        target_ids = [orders[0]["订单编号"], orders[1]["订单编号"]]
        created, skipped = quality_scan_service.transfer_orders(target_ids, handler="夜班-小王")
        assert skipped == []
        assert len(created) == 2
        record = quality_scan_service.list_transfers()[0]
        assert record["转交时评分"] == 76
        assert record["转交时异常率"] == "0.24%"
        assert record["扫描行数快照"] == 5000
        assert record["实测范围快照"]
        # 重复转交被幂等拦下。
        again, skipped_again = quality_scan_service.transfer_orders(target_ids)
        assert again == []
        assert set(skipped_again) == set(target_ids)

    def test_transfer_blocked_when_score_meets_threshold(self):
        # 构造一个零异常的实测批次，评分 100，不应允许转交。
        quality_scan_service.upsert_measured_result({
            "批次编号": "BATCH-CLEAN", "规则编号": "QUAL-CLEAN", "监控对象": "clean_tbl",
            "扫描行数": 100, "异常行数": 0, "实测范围": "clean_tbl 全量",
        })
        created, skipped = quality_scan_service.transfer_orders(["ORD-X"], batch_id="BATCH-CLEAN")
        assert created == []
        assert skipped  # 被门槛拦下


class TestRescanAfterDenominatorArrives:
    """补齐分母后重新扫描，新分数与历史转交并存。"""

    def test_rescan_recalculates_score_with_second_rule(self):
        run_quality_scan()
        # 先转交两笔，保留补扫前依据。
        orders = quality_scan_service.list_transferable_orders()
        quality_scan_service.transfer_orders([orders[0]["订单编号"]])

        # 补扫：准确性规则 2000 行 / 2 条异常。
        assert QUALITY_RESCAN_SAMPLES[0]["规则编号"] == "QUAL-ACCURACY"
        result = run_quality_rescan()
        # 合并口径：(12+2)/(5000+2000) = 0.20%，评分 80。
        assert result["total_scanned_rows"] == 7000
        assert result["total_anomalies"] == 14
        assert result["anomaly_rate_percent"] == "0.20%"
        assert result["quality_score"] == 80
        # 达到 80 分门槛，不再触发新的人工复核。
        assert result["needs_manual_review"] is False

        summary = quality_scan_service.summarize()
        assert summary["缺分母规则数"] == 0
        # 旧摘要转入存档，不再被当作缺分母阻塞项。
        archived_rules = {item["规则编号"] for item in summary["已补测摘要存档"]}
        assert "QUAL-ACCURACY" in archived_rules
        # 两条规则结果都可核对规则与实测范围。
        rule_codes = {r["规则编号"] for r in summary["规则结果"]}
        assert rule_codes == {"QUAL-COMPLETENESS", "QUAL-ACCURACY"}

    def test_historical_transfer_survives_rescan(self):
        run_quality_scan()
        orders = quality_scan_service.list_transferable_orders()
        quality_scan_service.transfer_orders([orders[0]["订单编号"]], handler="夜班-小王")
        run_quality_rescan()

        transfers = quality_scan_service.list_transfers()
        assert len(transfers) == 1
        record = transfers[0]
        # 补扫后仍能查到转交当时的原始依据：5000 行分母、76 分、0.24%。
        assert record["扫描行数快照"] == 5000
        assert record["转交时评分"] == 76
        assert record["转交时异常率"] == "0.24%"
        assert record["状态"] == "已转交"
        assert record["处理人"] == "夜班-小王"

    def test_pipeline_result_carries_rule_level_breakdown(self):
        run_quality_scan()
        run_quality_rescan()
        pipeline_rows = [r for r in list_pipeline_results() if r["pipeline_id"] == "quality_scan"]
        assert len(pipeline_rows) == 1  # 管道结果同样幂等，不重复追加
        payload = pipeline_rows[0]["payload"]
        assert len(payload["rule_results"]) == 2
        for rule in payload["rule_results"]:
            assert rule["扫描行数"] > 0
            assert rule["实测范围"]
        assert len(store.rows(TRANSFER_TABLE)) >= 0
