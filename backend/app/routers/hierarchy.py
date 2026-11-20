"""层级展开专用接口：校验层级、展开路径、按交付日期执行桥接分摊并查询明细。"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.dim_bridge import DimBridgeService
from app.services.hierarchy_expansion import (
    BridgeAllocationError,
    HierarchyCycleError,
    HierarchyError,
    build_hierarchy_snapshot,
    fact_partition_totals,
    hierarchy_versions,
    member_path_records,
    select_hierarchy_version,
    validate_hierarchy,
)
from app.store import store

router = APIRouter(prefix="/api/hierarchy", tags=["层级展开与桥接分摊"])


class ExpandPayload(BaseModel):
    delivery_date: str
    as_of: str | None = None
    fact_ids: list[str] | None = None
    reason: str = "桥接展开"


@router.get("/{hierarchy_name}/validation", response_model=dict)
def validate(
    hierarchy_name: str,
    effective_on: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """校验层级父子关系；有循环时返回循环成员而不是只给一句失败。"""
    try:
        return validate_hierarchy(hierarchy_name, effective_on=effective_on)
    except HierarchyCycleError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "cycles": exc.cycles, "循环成员": exc.cycles},
        ) from exc
    except HierarchyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{hierarchy_name}/paths", response_model=dict)
def paths(
    hierarchy_name: str,
    business_date: Annotated[str, Query()],
    version_label: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """按业务日期展开成员路径，返回适用的层级版本与适用日期。

    传 ``version_label`` 时固定用该版本快照展开；不传则用交付日期当时
    最新的版本，便于对照“当时交付”和“现在实时”的父子关系。
    """
    try:
        chosen = None
        if version_label:
            chosen = next(
                (
                    row for row in hierarchy_versions(hierarchy_name)
                    if str(row.get("层级版本")) == version_label
                ),
                None,
            )
            if chosen is None:
                raise HierarchyError(f"层级 {hierarchy_name} 没有版本 {version_label}")
        elif select_hierarchy_version(hierarchy_name, as_of=business_date) is not None:
            chosen = select_hierarchy_version(hierarchy_name, as_of=business_date)
        records = member_path_records(hierarchy_name, business_date, version=chosen)
    except HierarchyCycleError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "cycles": exc.cycles, "循环成员": exc.cycles},
        ) from exc
    except HierarchyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "business_date": business_date,
        "version": (chosen or {}).get("层级版本", "实时"),
        "items": records,
        "total": len(records),
    }


@router.get("/{hierarchy_name}/versions", response_model=dict)
def list_versions(hierarchy_name: str) -> dict[str, Any]:
    """列出层级已构建的版本，供桥接按交付日期选择和复算。"""
    try:
        rows = hierarchy_versions(hierarchy_name)
    except HierarchyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"hierarchy_name": hierarchy_name, "items": rows, "total": len(rows)}


@router.post("/{hierarchy_name}/versions", response_model=dict)
def build_version(hierarchy_name: str, built_on: str | None = Query(default=None)) -> dict[str, Any]:
    """校验并落一份层级版本快照，供桥接按交付日期引用。"""
    try:
        return build_hierarchy_snapshot(hierarchy_name, built_on=built_on)
    except HierarchyCycleError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "cycles": exc.cycles, "循环成员": exc.cycles},
        ) from exc
    except HierarchyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/bridges/{entry_id}/expansion", response_model=dict)
def expand(entry_id: int, payload: ExpandPayload) -> dict[str, Any]:
    """按层级构建交付的父子关系展开事实，登记父节点分摊结果。"""
    service = DimBridgeService()
    result, message = service.expand_facts(
        entry_id,
        payload.delivery_date,
        as_of=payload.as_of,
        fact_ids=payload.fact_ids,
        reason=payload.reason,
    )
    if result is None:
        raise HTTPException(status_code=422, detail=message)
    return {"ok": True, "message": message, "result": result}


@router.get("/bridges/{entry_id}/expansion", response_model=dict)
def expansion_details(
    entry_id: int,
    delivery_date: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """查看某个桥接的分摊明细，可按交付日期和状态过滤。"""
    entry = store.find("dim_bridge", entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"桥接表 {entry_id} 不存在或已归档")
    rows = [
        row for row in store.rows("bridge_expansion_detail")
        if row.get("桥接名称") == entry.get("桥接名称")
    ]
    if delivery_date:
        rows = [row for row in rows if str(row.get("交付日期")) == delivery_date]
    if status:
        rows = [row for row in rows if row.get("状态") == status]
    return {
        "bridge_name": entry.get("桥接名称"),
        "items": rows,
        "total": len(rows),
        "totals": fact_partition_totals(rows),
    }
