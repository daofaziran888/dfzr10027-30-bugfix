"""质量扫描接口：实测分母口径的异常率、评分、交班日报与人工复核转交。

接口分三组：
1. 扫描与结果：触发扫描、查询批次×规则实测结果与汇总（关联管道结果）。
2. 日报：按质量评分门槛输出交班日报与可转交订单。
3. 转交：订单转人工复核并保留可溯源的原始依据快照。
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.schemas import ActionResult
from app.services.pipeline_orchestrator import run_quality_scan
from app.services.quality_scan import quality_scan_service

router = APIRouter(prefix="/api/quality_scan", tags=["质量扫描"])


class MeasuredPayload(BaseModel):
    """批次×规则实测扫描结果上报载荷。"""

    批次编号: str
    规则编号: str
    监控对象: str
    校验类型: str | None = None
    扫描行数: int | None = None
    异常行数: int
    实测范围: str | None = None
    是否实测: bool = True
    空值率: float | None = None
    重复率: float | None = None
    异常订单: list[str] | None = None


class TransferPayload(BaseModel):
    """订单转交人工复核载荷。"""

    order_ids: list[str]
    handler: str | None = None
    remark: str | None = None
    batch_id: str | None = None


@router.post("/run", response_model=ActionResult)
def trigger_scan() -> ActionResult:
    """触发质量扫描管道，按实测分母重算异常率与评分。"""
    result = run_quality_scan()
    return ActionResult(ok=True, message="质量扫描已按实测行数完成", entry=result)


@router.get("/results")
def get_results(batch_id: str | None = Query(default=None, description="按批次编号过滤")) -> dict[str, object]:
    """查询每条批次×规则的实测结果，局部覆盖也能核对规则编号与实测范围。"""
    items = quality_scan_service.list_measured_results(batch_id)
    return {"items": items, "total": len(items)}


@router.post("/measured", response_model=ActionResult)
def submit_measured(payload: MeasuredPayload) -> ActionResult:
    """上报/补扫一条批次×规则结果；同批同规则重试幂等覆盖，只留一条有效结果。"""
    entry, missing = quality_scan_service.upsert_measured_result(payload.model_dump())
    if missing:
        return ActionResult(ok=False, message=f"字段不合法或缺失：{'、'.join(missing)}")
    return ActionResult(ok=True, message="实测扫描结果已登记", entry=entry)


@router.get("/summary")
def get_summary(batch_id: str | None = Query(default=None)) -> dict[str, object]:
    """汇总实测异常率与质量评分；缺分母的历史摘要单列、不充当分母。"""
    return quality_scan_service.summarize(batch_id)


@router.get("/issues")
def get_issues(batch_id: str | None = Query(default=None)) -> dict[str, object]:
    """质量问题清单：每条都带规则编号、实测扫描行数、异常数与实测范围。"""
    summary = quality_scan_service.summarize(batch_id)
    return {"items": summary["质量问题清单"], "total": len(summary["质量问题清单"])}


@router.get("/daily-report")
def get_daily_report(batch_id: str | None = Query(default=None)) -> dict[str, object]:
    """交班日报：按评分门槛决定是否人工复核，并区分订单级与整批覆盖口径。"""
    return quality_scan_service.build_daily_report(batch_id)


@router.get("/transferable")
def get_transferable(batch_id: str | None = Query(default=None)) -> dict[str, object]:
    """查询本班可转交人工复核的异常订单及其依据与覆盖范围。"""
    items = quality_scan_service.list_transferable_orders(batch_id)
    summary = quality_scan_service.summarize(batch_id)
    return {
        "items": items,
        "total": len(items),
        "质量评分": summary["质量评分"],
        "复核门槛": summary["复核门槛"],
        "需人工复核": summary["需人工复核"],
        "覆盖范围": "仅已实测规则命中的异常订单，不含缺分母规则的整批数据",
    }


@router.post("/transfers", response_model=ActionResult)
def create_transfer(payload: TransferPayload) -> ActionResult:
    """把订单转交人工复核，固化当时的规则、批次、实测范围与评分快照。"""
    created, skipped = quality_scan_service.transfer_orders(
        payload.order_ids, handler=payload.handler, remark=payload.remark, batch_id=payload.batch_id)
    if not created:
        return ActionResult(ok=False, message=f"没有可转交的订单：{ '、'.join(skipped) or '评分已达门槛' }")
    message = f"已转交 {len(created)} 笔订单人工复核"
    if skipped:
        message += f"，跳过 {len(skipped)} 笔（重复转交或缺少实测依据）"
    return ActionResult(ok=True, message=message, entry={"created": created, "skipped": skipped})


@router.get("/transfers")
def list_transfers() -> dict[str, object]:
    """查询历史转交记录；补扫重算后仍保留转交时的原始依据快照。"""
    items = quality_scan_service.list_transfers()
    return {"items": items, "total": len(items)}
