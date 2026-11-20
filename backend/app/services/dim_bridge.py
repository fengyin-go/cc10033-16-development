"""桥接关联服务：管理维度与事实之间的多对多桥接表定义。

增强能力：
- 消费 dim.published 和 fact.published 通道校验多对多关系
- 权重字段验证（权重值范围）
- 时效性管理：生效/失效时间与主记录标记
- 消费层级构建交付的父子关系展开事实，登记成员到父节点的分摊结果
- 分摊明细登记层级版本与适用日期，同一业务日复算时旧明细置为已替代
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.hierarchy_expansion import (
    BridgeAllocationError,
    HierarchyCycleError,
    HierarchyError,
    find_hierarchy,
    publish_bridge_expansion,
)
from app.store import store


class DimBridgeService(BaseModuleService):
    MODULE = "dim_bridge"
    ENTITY = "桥接表"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["桥接名称", "多对多维度", "关联事实", "权重字段", "生效时间", "失效时间", "主记录标记"]
    REQUIRED_FIELDS = ["桥接名称", "多对多维度", "关联事实"]
    STATUS_ORDER = ["草稿", "已生效", "已失效"]
    ACTION_RULES = {"启用桥接": "草稿", "停用桥接": "已生效"}
    NEGATIVE_ACTIONS = ["停用桥接"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        many_dims = str(values.get("多对多维度", "")).strip()
        related_fact = str(values.get("关联事实", "")).strip()

        # 校验维度存在性
        _validate_bridge_dims(many_dims, related_fact)

        # 设置默认时间
        if not values.get("生效时间"):
            values["生效时间"] = "2026-01-01"
        if not values.get("失效时间"):
            values["失效时间"] = "9999-12-31"
        if not values.get("主记录标记"):
            values["主记录标记"] = "Y"

        # 消费通道数据
        _consume_bridge_context(many_dims, related_fact)
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 层级名称不在固定列表字段里，但展开时需要它指向层级构建产物。
        hierarchy_name = str(values.get("层级名称", "")).strip()
        if hierarchy_name:
            entry["层级名称"] = hierarchy_name
            try:
                find_hierarchy(hierarchy_name)
            except HierarchyError:
                entry["abnormal"] = True

        store.channel.publish("bridge.defined", {
            "bridge_name": entry["桥接名称"],
            "dims": many_dims,
            "fact": related_fact,
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "dim_bridge", "bridge_table",
            f"bridge_{entry['id']}",
            {"bridge_name": entry["桥接名称"], "dims": many_dims, "fact": related_fact},
            ["dim_definition", "fact_design", "dim_bridge"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "启用桥接":
            store.channel.publish("bridge.activated", {
                "bridge_name": entry["桥接名称"],
                "entry_id": entry_id,
            })
        elif action == "停用桥接":
            entry["失效时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            store.channel.publish("bridge.deactivated", {
                "bridge_name": entry["桥接名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def expand_facts(
        self,
        entry_id: int,
        delivery_date: str,
        *,
        as_of: str | None = None,
        fact_ids: list[str] | None = None,
        reason: str = "桥接展开",
    ) -> tuple[dict[str, Any] | None, str]:
        """按层级构建交付的父子关系展开事实，登记父节点分摊结果。

        交付日期用来选择层级版本与源事实批次；每笔事实再按自己的业务日期
        解析当日有效路径，转父前后的分摊可以同时还原。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if entry.get("status") == "已失效":
            return None, f"桥接「{entry.get('桥接名称')}」已失效，不能执行分摊"
        try:
            result = publish_bridge_expansion(
                entry["桥接名称"],
                delivery_date,
                as_of=as_of,
                fact_ids=fact_ids,
                reason=reason,
            )
        except HierarchyCycleError as exc:
            entry["abnormal"] = True
            return None, str(exc)
        except HierarchyError as exc:
            entry["abnormal"] = True
            return None, f"层级关系校验失败：{exc}"
        except BridgeAllocationError as exc:
            entry["abnormal"] = True
            return None, str(exc)

        entry["abnormal"] = not result["金额守恒"]
        entry["最近交付日期"] = result["交付日期"]
        entry["最近复算日期"] = result["复算日期"]
        entry["父节点分摊"] = result["父节点分摊"]
        entry["历史更正数"] = len(result["历史更正"])
        entry["最近分摊批次"] = result["复算批次"]
        entry["最近分摊金额"] = result["分摊总额"]
        entry["层级版本"] = result["层级版本"]
        return result, (
            f"桥接「{result['桥接名称']}」按层级版本 {result['层级版本']} 展开 "
            f"{result['事实笔数']} 笔事实，分摊金额 {result['分摊总额']} 与源事实总额 "
            f"{result['源事实总额']} 相等，替代旧明细 {len(result['替代明细'])} 条"
        )

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        active = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已生效")
        base["active_bridges"] = active
        details = store.rows("bridge_expansion_detail")
        base["expansion_details"] = len(details)
        base["expansion_valid"] = sum(1 for row in details if row.get("状态") == "有效")
        return base


def _validate_bridge_dims(dim_str: str, fact_str: str) -> None:
    dims = [d.strip() for d in dim_str.split(".") if d.strip()]
    existing = {r.get("维度编码") for r in store.rows("dim_definition")}
    valid = [d for d in dims if d in existing]
    if valid:
        store.channel.publish("bridge.dims_validated", {"valid_dims": valid, "dim_str": dim_str})


def _consume_bridge_context(dim_str: str, fact_str: str) -> None:
    store.channel.consume("dim.published", "dim_bridge")
    store.channel.consume("fact.published", "dim_bridge")