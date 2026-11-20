"""质量扫描业务规则：实测分母、批次幂等、评分与交班日报。

设计要点（对应分母占位值缺陷的修复）：

1. 每条扫描结果都归属「批次 × 规则」，且必须记录实测扫描行数与实测范围；
   异常数和分母永远取自同一次扫描的同一批实测范围，禁止再用 1000 行之类的
   占位值估算分母。
2. 同批次同规则重复上报（重试）按幂等覆盖处理，同一 key 只保留一条有效结果，
   不会因为重试把异常数、扫描行数重复累加。
3. 只有异常行数、没有扫描行数的历史摘要允许登记，但状态标记为「缺分母」，
   不进入异常率与评分计算，也不产生可转交订单。
4. 质量评分 = 100 - 汇总异常率(%) × 100（即 100 - 异常数/扫描行数 × 10000），
   汇总只合并带实测分母的结果；局部覆盖时逐条返回所用规则与实测范围，可核对。
5. 交班日报按评分门槛（默认 80 分）决定是否触发人工复核；转交订单会固化
   当时的规则、批次、实测范围与异常依据，补扫重算后历史转交记录仍可溯源。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.fixtures import QUALITY_CHECK_SAMPLES
from app.store import store

# 低于该评分需要把相关订单数据送去人工复核。
REVIEW_SCORE_THRESHOLD = 80
# 实测结果存放的 store 命名空间。
MEASURED_TABLE = "_quality_measured_results"
# 订单转交记录存放的 store 命名空间。
TRANSFER_TABLE = "_quality_transfers"

MEASURED_REQUIRED_FIELDS = ["批次编号", "规则编号", "监控对象", "异常行数"]


class QualityScanService:
    """封装质量样本上报、评分汇总、日报与订单转交的全部业务判断。"""

    # ------------------------------------------------------------------
    # 样本上报（批次 × 规则，幂等）
    # ------------------------------------------------------------------
    def seed_fixture_samples(self) -> int:
        """把 fixtures 里的本班样本灌入实测结果表，返回写入的有效结果条数。

        幂等：已存在的批次×规则直接覆盖，因此管道重复执行不会产生重复结果。
        """
        count = 0
        for sample in QUALITY_CHECK_SAMPLES:
            self.upsert_measured_result(sample)
            if bool(sample.get("是否实测")):
                count += 1
        return count

    def upsert_measured_result(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记或覆盖一条批次×规则扫描结果。

        实测结果必须带正整数扫描行数；缺扫描行数时按「历史摘要」处理，
        只登记异常数，绝不允许它充当分母。
        """
        missing = [
            field for field in MEASURED_REQUIRED_FIELDS
            if payload.get(field) is None
            or (isinstance(payload.get(field), str) and not payload.get(field).strip())
        ]
        if missing:
            return None, missing

        batch_id = str(payload["批次编号"]).strip()
        rule_code = str(payload["规则编号"]).strip()
        try:
            anomaly_count = int(payload["异常行数"])
        except (TypeError, ValueError):
            return None, ["异常行数"]
        if anomaly_count < 0:
            return None, ["异常行数"]

        scanned_raw = payload.get("扫描行数")
        is_measured = bool(payload.get("是否实测", True))
        scanned_rows: int | None
        if scanned_raw in (None, ""):
            # 没有分母：只能作为历史摘要，不能参与比率计算。
            scanned_rows = None
            is_measured = False
        else:
            try:
                scanned_rows = int(scanned_raw)
            except (TypeError, ValueError):
                return None, ["扫描行数"]
            if scanned_rows <= 0:
                return None, ["扫描行数"]
            if anomaly_count > scanned_rows:
                return None, ["异常行数"]
            is_measured = True

        table = store.rows(MEASURED_TABLE)
        now = datetime.now().isoformat()
        for row in table:
            if row["批次编号"] == batch_id and row["规则编号"] == rule_code:
                # 同批同规则重试：原地覆盖，只保留一个有效结果。
                row.update(self._build_result(payload, batch_id, rule_code, scanned_rows,
                                              anomaly_count, is_measured, now))
                row["更新时间"] = now
                return dict(row), []

        entry = {"id": self._next_id(table)}
        entry.update(self._build_result(payload, batch_id, rule_code, scanned_rows,
                                        anomaly_count, is_measured, now))
        entry["创建时间"] = now
        entry["更新时间"] = now
        table.append(entry)
        return dict(entry), []

    def _build_result(
        self,
        payload: dict[str, Any],
        batch_id: str,
        rule_code: str,
        scanned_rows: int | None,
        anomaly_count: int,
        is_measured: bool,
        now: str,
    ) -> dict[str, Any]:
        orders = payload.get("异常订单") or []
        return {
            "批次编号": batch_id,
            "规则编号": rule_code,
            "监控对象": str(payload.get("监控对象", "")).strip(),
            "校验类型": str(payload.get("校验类型", "")).strip(),
            "扫描行数": scanned_rows,
            "异常行数": anomaly_count,
            "实测范围": str(payload.get("实测范围", "")).strip(),
            "是否实测": is_measured,
            "数据状态": "实测有效" if is_measured else "缺分母",
            "空值率": payload.get("空值率"),
            "重复率": payload.get("重复率"),
            "异常订单": [str(order_id) for order_id in orders],
            "最近扫描时间": now,
        }

    def list_measured_results(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        """返回扫描结果，可按批次过滤；每条都带规则编号与实测范围便于核对。"""
        rows = store.rows(MEASURED_TABLE)
        if batch_id:
            rows = [row for row in rows if row.get("批次编号") == batch_id]
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 评分汇总（只用实测分母）
    # ------------------------------------------------------------------
    def summarize(self, batch_id: str | None = None) -> dict[str, Any]:
        """按实测范围汇总异常率与评分。

        - 实测结果：异常数与扫描行数成对累加，参与异常率、评分与问题清单。
        - 缺分母摘要：单独列出，只展示异常数，不进入任何分母。
        - 局部覆盖（只扫了部分批次/规则）也如实返回每条所用规则与实测范围。
        """
        rows = self.list_measured_results(batch_id)
        measured = [row for row in rows if row.get("是否实测") and row.get("扫描行数")]
        measured_rules = {row["规则编号"] for row in measured}
        # 只有始终没有任何实测分母的规则才算「缺分母」；规则一旦补扫，
        # 旧摘要只作存档，不再阻塞整批结论。
        missing = [row for row in rows if not row.get("是否实测")
                   and row["规则编号"] not in measured_rules]
        archived_summaries = [row for row in rows if not row.get("是否实测")
                              and row["规则编号"] in measured_rules]

        rule_results: list[dict[str, Any]] = []
        total_scanned = 0
        total_anomalies = 0
        issue_list: list[dict[str, Any]] = []

        for row in measured:
            scanned = int(row["扫描行数"])
            anomalies = int(row["异常行数"])
            total_scanned += scanned
            total_anomalies += anomalies
            rate = anomalies / scanned if scanned else 0.0
            rule_result = {
                "批次编号": row["批次编号"],
                "规则编号": row["规则编号"],
                "监控对象": row["监控对象"],
                "校验类型": row["校验类型"],
                "扫描行数": scanned,
                "异常行数": anomalies,
                "异常率": round(rate, 6),
                "异常率百分比": f"{rate * 100:.2f}%",
                "实测范围": row["实测范围"],
                "数据状态": row["数据状态"],
                "最近扫描时间": row.get("最近扫描时间"),
            }
            rule_results.append(rule_result)
            if anomalies > 0:
                issue_list.append({
                    **rule_result,
                    "空值率": row.get("空值率"),
                    "重复率": row.get("重复率"),
                    "异常订单数": len(row.get("异常订单") or []),
                })

        overall_rate = total_anomalies / total_scanned if total_scanned else 0.0
        # 评分口径与原实现一致：100 - 异常率×10000，下限 0；分母为 0 时不给虚高分数。
        score = max(0, 100 - round(overall_rate * 10000)) if total_scanned else 0

        missing_denominator = [
            {
                "批次编号": row["批次编号"],
                "规则编号": row["规则编号"],
                "监控对象": row["监控对象"],
                "校验类型": row["校验类型"],
                "异常行数": int(row["异常行数"]),
                "扫描行数": None,
                "实测范围": row["实测范围"],
                "数据状态": "缺分母",
                "说明": "仅有历史异常摘要，分母未到，补扫前不参与异常率与评分",
            }
            for row in missing
        ]

        archived = [
            {
                "批次编号": row["批次编号"],
                "规则编号": row["规则编号"],
                "异常行数": int(row["异常行数"]),
                "扫描行数": None,
                "数据状态": "已补测-摘要存档",
                "说明": "该规则已补齐实测分母，本历史摘要仅存档，不作为评分依据",
            }
            for row in archived_summaries
        ]

        return {
            "scan_time": datetime.now().isoformat(),
            "批次编号": batch_id,
            "覆盖批次": sorted({row["批次编号"] for row in measured}),
            "实测规则数": len(measured),
            "缺分母规则数": len(missing),
            "总扫描行数": total_scanned,
            "总异常行数": total_anomalies,
            "实测异常率": round(overall_rate, 6),
            "实测异常率百分比": f"{overall_rate * 100:.2f}%",
            "质量评分": score,
            "需人工复核": score < REVIEW_SCORE_THRESHOLD if total_scanned else True,
            "复核门槛": REVIEW_SCORE_THRESHOLD,
            "规则结果": rule_results,
            "质量问题清单": issue_list,
            "缺分母摘要": missing_denominator,
            "已补测摘要存档": archived,
        }

    # ------------------------------------------------------------------
    # 交班日报
    # ------------------------------------------------------------------
    def build_daily_report(self, batch_id: str | None = None) -> dict[str, Any]:
        """生成交班日报：按质量评分决定是否触发人工复核。

        日报区分两层覆盖口径：
        - 可立即处理的异常订单：来自已实测规则，处理意见只覆盖这些订单；
        - 整批数据：要等缺分母规则补扫完成后才能给出整批结论。
        """
        summary = self.summarize(batch_id)
        actionable_orders = self.list_transferable_orders(batch_id)
        missing_rules = [item["规则编号"] for item in summary["缺分母摘要"]]

        handling_opinion = (
            f"将 {len(actionable_orders)} 笔已实测命中的异常订单转人工复核；"
            "处理意见仅覆盖上述订单，不代表整批数据结论。"
            if actionable_orders else
            "当前已实测规则未命中可立即处理的异常订单。"
        )
        if missing_rules:
            handling_opinion += (
                f"规则 {'、'.join(missing_rules)} 分母迟到，整批数据结论需在补扫后补充。"
            )

        return {
            "报表编号": f"QUAL-DAILY-{datetime.now().strftime('%Y%m%d')}",
            "生成时间": datetime.now().isoformat(),
            "批次编号": batch_id,
            "质量评分": summary["质量评分"],
            "复核门槛": REVIEW_SCORE_THRESHOLD,
            "实测异常率百分比": summary["实测异常率百分比"],
            "总扫描行数": summary["总扫描行数"],
            "总异常行数": summary["总异常行数"],
            "需人工复核": summary["需人工复核"],
            "复核结论": (
                f"质量评分 {summary['质量评分']} 分低于 {REVIEW_SCORE_THRESHOLD} 分，触发人工复核"
                if summary["需人工复核"]
                else f"质量评分 {summary['质量评分']} 分达到 {REVIEW_SCORE_THRESHOLD} 分门槛，无需整批复核"
            ),
            "覆盖范围说明": (
                "处理意见仅覆盖已实测规则命中的异常订单，不是整批数据结论"
                if missing_rules else "全部规则均已实测，结论覆盖本班全部规则范围"
            ),
            "可处理异常订单数": len(actionable_orders),
            "缺分母规则": missing_rules,
            "规则结果": summary["规则结果"],
            "质量问题清单": summary["质量问题清单"],
            "缺分母摘要": summary["缺分母摘要"],
            "处理意见": handling_opinion,
        }

    # ------------------------------------------------------------------
    # 订单转交（人工复核）
    # ------------------------------------------------------------------
    def list_transferable_orders(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        """列出本班可转交人工复核的异常订单。

        只有带实测分母的结果能提供可核对依据；缺分母摘要没有订单级证据，
        不产生可转交订单。评分低于门槛时才允许转交。
        """
        summary = self.summarize(batch_id)
        if not summary["需人工复核"]:
            return []
        transferable: list[dict[str, Any]] = []
        for row in self.list_measured_results(batch_id):
            if not row.get("是否实测") or int(row.get("异常行数") or 0) <= 0:
                continue
            for order_id in row.get("异常订单") or []:
                transferable.append({
                    "订单编号": order_id,
                    "批次编号": row["批次编号"],
                    "规则编号": row["规则编号"],
                    "监控对象": row["监控对象"],
                    "校验类型": row["校验类型"],
                    "扫描行数": int(row["扫描行数"]),
                    "异常行数": int(row["异常行数"]),
                    "实测范围": row["实测范围"],
                    "依据": self._evidence_text(row),
                })
        return transferable

    def transfer_orders(
        self,
        order_ids: list[str],
        *,
        handler: str | None = None,
        remark: str | None = None,
        batch_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """把异常订单转交人工复核，并固化转交当时的评分与证据快照。

        快照不随后续补扫变化：补扫重算后仍可凭转交记录查到原来的规则、
        批次、实测范围与当时的评分依据。
        """
        ids = [str(order_id).strip() for order_id in order_ids if str(order_id or "").strip()]
        if not ids:
            return [], ["未选择需要转交的订单"]

        summary = self.summarize(batch_id)
        if not summary["需人工复核"]:
            return [], [f"质量评分 {summary['质量评分']} 分已达门槛，无需人工复核"]

        candidates = {item["订单编号"]: item for item in self.list_transferable_orders(batch_id)}
        table = store.rows(TRANSFER_TABLE)
        existing = {row["订单编号"] for row in table}
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        now = datetime.now().isoformat()

        for order_id in ids:
            if order_id in existing:
                skipped.append(order_id)
                continue
            source = candidates.get(order_id)
            if source is None:
                skipped.append(order_id)
                continue
            entry = {
                "id": self._next_id(table),
                "订单编号": order_id,
                "批次编号": source["批次编号"],
                "规则编号": source["规则编号"],
                "监控对象": source["监控对象"],
                "校验类型": source["校验类型"],
                "扫描行数快照": source["扫描行数"],
                "异常行数快照": source["异常行数"],
                "实测范围快照": source["实测范围"],
                "转交时评分": summary["质量评分"],
                "转交时异常率": summary["实测异常率百分比"],
                "复核门槛": REVIEW_SCORE_THRESHOLD,
                "依据快照": source["依据"],
                "处理人": (handler or "值班员").strip(),
                "备注": (remark or "评分低于门槛，转人工复核").strip(),
                "状态": "已转交",
                "转交时间": now,
            }
            table.append(entry)
            created.append(dict(entry))
        return created, skipped

    def list_transfers(self) -> list[dict[str, Any]]:
        """返回全部历史转交记录（含补扫前的原始依据快照）。"""
        return [dict(row) for row in store.rows(TRANSFER_TABLE)]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _evidence_text(self, row: dict[str, Any]) -> str:
        return (
            f"规则 {row['规则编号']}（{row['校验类型']}）在批次 {row['批次编号']} 的"
            f"{row['实测范围']} 中扫描 {row['扫描行数']} 行发现 {row['异常行数']} 条异常"
        )

    def _next_id(self, table: list[dict[str, Any]]) -> int:
        return max((int(row.get("id", 0)) for row in table), default=0) + 1


quality_scan_service = QualityScanService()
