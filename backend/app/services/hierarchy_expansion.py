"""层级展开与桥接分摊引擎。

层级构建和桥接关联原本只有关系定义，没有可供事实分摊使用的实际数据。
本模块补齐这条链路：

1. 层级关系校验：递归检测父子关系中的循环，错误信息直接列出循环成员。
2. 成员路径展开：按业务日期与层级版本解析每个成员的祖先路径和适用日期，
   重复的祖先路径只保留一条，避免同一父节点被全额记两次。
3. 桥接事实分摊：把每笔源事实按父节点权重展开，逐笔校验展开金额之和与
   源事实金额相等。
4. 版本与历史更正：分摊明细登记层级版本、当日有效路径和适用日期；同一
   事实复算时旧明细置为“已替代”，任一事实不会同时计入两个版本。

日期区间统一使用闭区间（生效日期、失效日期都包含当天）。复算日期决定使用
哪个层级版本（``构建日期 <= 复算日期`` 的最新版本），每笔事实再按自己的业务
日期过滤该版本里的分段，得到当日有效路径。这样既能还原当时交付的版本，
也能在成员月中转父后说明“哪天走哪条路径”、并登记历史更正。
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from app.store import store

DATE_MIN = date(1900, 1, 1)
DATE_MAX = date(9999, 12, 31)
MONEY = Decimal("0.01")
WEIGHT = Decimal("0.0001")

HIERARCHY_MODULE = "hierarchy_build"
MEMBER_MODULE = "hierarchy_member"
VERSION_MODULE = "hierarchy_version"
BRIDGE_MODULE = "dim_bridge"
WEIGHT_MODULE = "bridge_member_weight"
SOURCE_FACT_MODULE = "bridge_source_fact"
DETAIL_MODULE = "bridge_expansion_detail"


class HierarchyError(ValueError):
    """层级关系不可用（缺少成员数据、版本缺失等）。"""


class HierarchyCycleError(HierarchyError):
    """层级关系存在循环引用，异常中携带循环成员，便于直接定位。"""

    def __init__(self, hierarchy_name: str, cycles: list[list[str]]):
        self.hierarchy_name = hierarchy_name
        self.cycles = cycles
        chains = "；".join(" → ".join(cycle) for cycle in cycles)
        super().__init__(
            f"层级关系校验失败：{hierarchy_name} 检测到循环引用，循环成员：{chains}"
        )


class BridgeAllocationError(ValueError):
    """桥接分摊无法保持金额守恒，或缺少当日有效路径。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def as_date(value: Any, default: date | None = None) -> date:
    """把常见日期格式统一成 date；空值按需回退到默认值。"""
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise ValueError("日期不能为空")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法识别的日期：{value!r}")


def as_money(value: Any) -> Decimal:
    """金额统一成两位小数，避免浮点误差影响守恒校验。"""
    try:
        return Decimal(str(value if value not in (None, "") else "0")).quantize(
            MONEY, rounding=ROUND_HALF_UP
        )
    except Exception as exc:  # noqa: BLE001 - 需要把脏数据转成明确错误
        raise BridgeAllocationError(f"无法识别金额：{value!r}") from exc


def as_weight(value: Any) -> Decimal:
    try:
        weight = Decimal(str(value if value not in (None, "") else "0"))
    except Exception as exc:  # noqa: BLE001
        raise BridgeAllocationError(f"无法识别权重：{value!r}") from exc
    return weight.quantize(WEIGHT, rounding=ROUND_HALF_UP)


def today() -> date:
    return datetime.now(timezone.utc).date()


def _in_range(day: date, start: date, end: date) -> bool:
    return start <= day <= end


def _range_of(row: dict[str, Any]) -> tuple[date, date]:
    start = as_date(row.get("生效日期"), DATE_MIN)
    end = as_date(row.get("失效日期"), DATE_MAX)
    return start, end


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _max_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0)


def find_hierarchy(name_or_id: str | int) -> dict[str, Any]:
    """按名称或 id 查找层级定义。"""
    rows = store.rows(HIERARCHY_MODULE)
    if isinstance(name_or_id, int) or str(name_or_id).isdigit():
        entry_id = int(name_or_id)
        for row in rows:
            if int(row.get("id", 0)) == entry_id:
                return row
    for row in rows:
        if str(row.get("层级名称", "")) == str(name_or_id):
            return row
    raise HierarchyError(f"未找到层级关系：{name_or_id}")


def hierarchy_members(hierarchy_name: str) -> list[dict[str, Any]]:
    """取层级下的父子关系事实；没有实际关系数据时给出明确错误。"""
    rows = [
        row for row in store.rows(MEMBER_MODULE)
        if str(row.get("层级名称", "")) == str(hierarchy_name)
    ]
    if not rows:
        raise HierarchyError(
            f"层级关系 {hierarchy_name} 缺少成员父子关系数据，无法展开事实分摊"
        )
    return [deepcopy(row) for row in rows]


def hierarchy_versions(hierarchy_name: str) -> list[dict[str, Any]]:
    """层级已发布的构建版本，按构建日期排序。"""
    return [deepcopy(row) for row in _version_rows(hierarchy_name)]


def resolve_hierarchy_reference(reference: str) -> str:
    """桥接可以写层级名称，也可以写多对多维度编码；后者要求该维度只有一个层级。"""
    text = str(reference).strip()
    if not text:
        raise HierarchyError("桥接未指定可展开的层级关系")
    try:
        return str(find_hierarchy(text).get("层级名称", ""))
    except HierarchyError:
        matched = [
            row for row in store.rows(HIERARCHY_MODULE)
            if str(row.get("所属维度", "")) == text
        ]
        if len(matched) == 1:
            return str(matched[0].get("层级名称", ""))
        if len(matched) > 1:
            names = "、".join(str(row.get("层级名称", "")) for row in matched)
            raise HierarchyError(f"维度 {text} 有多个层级（{names}），请在桥接中指定层级名称")
        raise HierarchyError(f"未找到层级关系：{reference}")


# ---------------------------------------------------------------------------
# 1. 层级关系校验
# ---------------------------------------------------------------------------


def validate_hierarchy(
    hierarchy: str | int,
    *,
    effective_on: date | str | None = None,
) -> dict[str, Any]:
    """校验层级父子关系，返回可用于展开的节点索引。

    校验内容：
    - 成员编码非空；
    - 父节点必须能在同一层级数据中解析（允许根节点父编码为空）；
    - 父子关系不能成环。发现环时异常信息会直接列出环内成员。

    ``effective_on`` 指定业务日时只校验当天生效的分段；不指定时按每条
    分段各自的生效日期逐日探测，避免把不同时期的历史分段拼成假环。
    """
    entry = find_hierarchy(hierarchy)
    name = str(entry.get("层级名称", ""))
    day = as_date(effective_on) if effective_on else None
    members = hierarchy_members(name)

    for row in members:
        if not str(row.get("成员编码", "")).strip():
            raise HierarchyError(f"层级关系 {name} 存在缺少成员编码的记录：{row}")

    probe_days = [day] if day else sorted({
        as_date(row.get("生效日期"), DATE_MIN) for row in members
    })

    labels: dict[str, str] = {}
    for row in members:
        code = str(row.get("成员编码", ""))
        labels.setdefault(code, str(row.get("节点名称", "") or code))
    last_edges: dict[str, str] = {}
    for probe in probe_days:
        parents = _active_paths(members, probe)[0]
        last_edges = parents
        known = set(parents)
        dangling = sorted(
            f"{member} → {parent}"
            for member, parent in parents.items()
            if parent and parent not in known
        )
        if dangling:
            raise HierarchyError(
                f"层级关系校验失败：{name} 在 {probe.isoformat()} 的父节点未在同一层级中定义："
                f"{'、'.join(dangling)}"
            )
        cycles = _find_cycles(parents)
        if cycles:
            raise HierarchyCycleError(name, cycles)

    return {
        "层级名称": name,
        "所属维度": entry.get("所属维度", ""),
        "校验日期": day.isoformat() if day else "",
        "探测日期": [probe.isoformat() for probe in probe_days],
        "成员数": len(last_edges),
        "根节点": sorted(m for m, p in last_edges.items() if not p),
        "节点名称": labels,
        "边": [
            {"成员编码": member, "父节点编码": parent}
            for member, parent in sorted(last_edges.items())
        ],
    }


def _find_cycles(edges: dict[str, str]) -> list[list[str]]:
    """在单父指针森林中找出全部循环；每个循环以有序成员列表返回。

    从每个未处理节点顺父指针上溯：遇到本轮路径上的节点即成环，遇到
    其他轮已确认无环的节点则安全。不能让“已访问”直接短路，否则从环外
    子节点进入环时会被前面的根节点误判为无环。
    """
    cycles: list[list[str]] = []
    done: set[str] = set()  # 已确认不在环上的节点
    for start in sorted(edges):
        if start in done:
            continue
        path: list[str] = []
        index: dict[str, int] = {}
        node = start
        while node and node not in done:
            if node in index:
                cycle = path[index[node]:]
                cycles.append(cycle + [node])
                break
            index[node] = len(path)
            path.append(node)
            node = edges.get(node, "")
        done.update(path)
    return cycles


def _parent_map(validated: dict[str, Any]) -> dict[str, str]:
    return {edge["成员编码"]: edge["父节点编码"] for edge in validated["边"]}


def member_paths(
    hierarchy: str | int,
    *,
    effective_on: date | str | None = None,
) -> dict[str, list[str]]:
    """展开每个成员到根的祖先路径（含自身，根在前）。

    同一路径只保留一次：多成员共享同一个祖先时不会产生重复路径记录，
    后续按父节点分摊也不会把同一父节点全额重复计入。
    """
    validate_hierarchy(hierarchy, effective_on=effective_on)
    day = as_date(effective_on) if effective_on else today()
    records = member_path_records(hierarchy, day)
    return {row["成员编码"]: row["祖先路径"] for row in records}


# ---------------------------------------------------------------------------
# 2. 层级版本：把某次构建交付的父子关系固定成快照
# ---------------------------------------------------------------------------


def _version_rows(hierarchy_name: str) -> list[dict[str, Any]]:
    rows = [
        row for row in store.rows(VERSION_MODULE)
        if str(row.get("层级名称", "")) == str(hierarchy_name)
    ]
    return sorted(rows, key=lambda r: (str(r.get("构建日期", "")), int(r.get("id", 0))))


def next_version_label(hierarchy_name: str) -> str:
    return f"v{len(_version_rows(hierarchy_name)) + 1}"


def build_hierarchy_snapshot(
    hierarchy: str | int,
    *,
    built_on: date | str | None = None,
    version_label: str | None = None,
    reason: str = "构建层级",
) -> dict[str, Any]:
    """校验层级关系并落一份版本快照，供桥接展开事实时使用。

    版本记录同时保存边和路径，之后即使成员父子关系被修改，也能按版本
    复算出“当时交付”的父节点分摊结果。
    """
    entry = find_hierarchy(hierarchy)
    name = str(entry.get("层级名称", ""))
    built_day = as_date(built_on, today())
    validated = validate_hierarchy(name)

    edges: list[dict[str, Any]] = []
    for row in hierarchy_members(name):
        # 版本只收录构建日之前已录入的关系：成员月中转父后重建，
        # 新版本才会带上新边，历史版本仍保留当时的父子关系。
        recorded = as_date(row.get("录入日期"), DATE_MIN)
        if recorded > built_day:
            continue
        start, end = _range_of(row)
        edges.append({
            "成员编码": str(row.get("成员编码", "")).strip(),
            "成员名称": str(row.get("节点名称", "") or row.get("成员编码", "")).strip(),
            "父节点编码": str(row.get("父节点编码", "")).strip(),
            "父节点名称": str(row.get("父节点名称", "") or row.get("父节点编码", "")).strip(),
            "生效日期": start.isoformat(),
            "失效日期": end.isoformat(),
            "录入日期": recorded.isoformat(),
        })

    version = {
        "id": _max_id(VERSION_MODULE) + 1,
        "层级名称": name,
        "层级版本": version_label or next_version_label(name),
        "构建日期": built_day.isoformat(),
        "构建类型": reason,
        "成员数": len(edges),
        "边快照": edges,
        "根节点": validated["根节点"],
        "status": "已发布",
        "pending": False,
        "abnormal": False,
        "created_at": _now_text(),
    }
    store.insert(VERSION_MODULE, version)
    store.channel.publish("hierarchy.version_published", {
        "hierarchy_name": name,
        "version": version["层级版本"],
        "built_on": version["构建日期"],
        "member_count": version["成员数"],
        "entry_id": version["id"],
    })
    store.artifacts.register(
        "hierarchy_build", "hierarchy_expansion",
        f"hierarchy_expansion_{version['id']}",
        {"hierarchy_name": name, "version": version["层级版本"], "built_on": version["构建日期"]},
        ["hierarchy_build", "hierarchy_member"],
    )
    return version


def select_hierarchy_version(
    hierarchy: str | int,
    *,
    as_of: date | str | None = None,
) -> dict[str, Any] | None:
    """选择交付日期当天有效的层级版本（构建日期不晚于交付日期的最新版本）。"""
    name = str(find_hierarchy(hierarchy).get("层级名称", ""))
    day = as_date(as_of, today())
    candidates = [
        row for row in _version_rows(name)
        if str(row.get("status", "已发布")) != "已撤销"
        and as_date(row.get("构建日期"), DATE_MIN) <= day
    ]
    return candidates[-1] if candidates else None


def _active_paths(edges: Iterable[dict[str, Any]], day: date) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, Any]]]:
    """取业务日当天生效的父子边，按成员分组后再按分段先后收敛。

    同一成员可能有多条历史分段（如月中转父），只保留当天那一段；
    同一天重叠的分段以最晚生效的为准，避免同一成员当天挂到两个父节点。
    """
    candidates: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        start = as_date(edge.get("生效日期"), DATE_MIN)
        end = as_date(edge.get("失效日期"), DATE_MAX)
        if not _in_range(day, start, end):
            continue
        member = str(edge.get("成员编码", ""))
        candidates.setdefault(member, []).append(edge)

    parents: dict[str, str] = {}
    labels: dict[str, str] = {}
    active: dict[str, dict[str, Any]] = {}
    for member, rows in candidates.items():
        chosen = max(
            rows,
            key=lambda row: (as_date(row.get("生效日期"), DATE_MIN), str(row.get("录入日期", ""))),
        )
        parent = str(chosen.get("父节点编码", ""))
        parents[member] = parent
        labels.setdefault(
            member,
            str(chosen.get("成员名称") or chosen.get("节点名称") or member),
        )
        if parent:
            labels.setdefault(
                parent,
                str(chosen.get("父节点名称") or parent),
            )
        active[member] = chosen
    return parents, labels, active


def _walk_paths(parents: dict[str, str]) -> dict[str, list[str]]:
    """路径展开（返回成员编码链）；发现环立即报出循环成员。"""
    cycles = _find_cycles(parents)
    if cycles:
        raise HierarchyCycleError("层级关系", cycles)
    result: dict[str, list[str]] = {}
    for member in sorted(parents):
        chain: list[str] = []
        node = member
        while node:
            chain.append(node)
            node = parents.get(node, "")
        chain.reverse()
        result[member] = chain
    return result


def member_path_records(
    hierarchy: str | int,
    business_date: date | str,
    *,
    version: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按业务日期计算每个成员的路径、适用日期和层级版本。"""
    entry = find_hierarchy(hierarchy)
    name = str(entry.get("层级名称", ""))
    day = as_date(business_date)

    # 显式传入版本时按版本快照展开（桥接交付用）；
    # 不传版本时按实时分段数据展开，便于查看“业务日当天的有效路径”。
    if version is not None:
        edges = version.get("边快照") or []
        version_label = str(version.get("层级版本", ""))
        version_source = "版本快照"
        version_id = version.get("id")
    else:
        edges = hierarchy_members(name)
        version_label = "实时"
        version_source = "实时数据"
        version_id = None

    parents, labels, active = _active_paths(edges, day)
    paths = _walk_paths(parents)

    records: list[dict[str, Any]] = []
    for member in sorted(paths):
        edge = active.get(member, {})
        codes = paths[member]
        path = [labels.get(code, code) for code in codes]
        records.append({
            "层级名称": name,
            "成员编码": member,
            "成员名称": labels.get(member, member),
            "父节点编码": parents.get(member, ""),
            "父节点名称": labels.get(parents.get(member, ""), ""),
            "祖先路径": path,
            "祖先路径编码": codes,
            "祖先路径文本": "/".join(path),
            "祖先路径键": "|".join(path),
            "层级深度": len(path),
            "适用日期": day.isoformat(),
            "路径有效期起": str(edge.get("生效日期", "")),
            "路径有效期止": str(edge.get("失效日期", "")),
            "层级版本": version_label,
            "版本来源": version_source,
            "版本记录ID": version_id,
        })
    return records


def resolve_member_paths(
    hierarchy: str | int,
    business_date: date | str,
    *,
    version: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """把成员路径记录整理成以成员编码为键的索引。"""
    return {
        row["成员编码"]: row
        for row in member_path_records(hierarchy, business_date, version=version)
    }


# ---------------------------------------------------------------------------
# 3. 桥接分摊
# ---------------------------------------------------------------------------


def _bridge_name(bridge: str | int) -> dict[str, Any]:
    rows = store.rows(BRIDGE_MODULE)
    if isinstance(bridge, int) or str(bridge).isdigit():
        for row in rows:
            if int(row.get("id", 0)) == int(bridge):
                return row
    for row in rows:
        if str(row.get("桥接名称", "")) == str(bridge):
            return row
    raise BridgeAllocationError(f"未找到桥接定义：{bridge}")


def _bridge_weights(bridge_name: str, business_date: date) -> list[dict[str, Any]]:
    rows = []
    for row in store.rows(WEIGHT_MODULE):
        if str(row.get("桥接名称", "")) != bridge_name:
            continue
        start, end = _range_of(row)
        if _in_range(business_date, start, end):
            rows.append(deepcopy(row))
    return rows


def _dedupe_targets(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """同一成员指向同一父节点的权重只保留一条，其余作为重复路径记录。

    这就是“重复祖先路径不能把桥接事实全额记两遍”的落点：先去重再分摊，
    重复记录不会参与金额计算，但在结果里留痕便于排查桥接表脏数据。
    """
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("成员编码", "")), str(row.get("父节点编码", "")))
        if key in kept:
            duplicates.append(row)
            continue
        kept[key] = row
    return list(kept.values()), duplicates


def allocate_fact_amount(
    *,
    fact_id: str,
    member: str,
    amount: Any,
    bridge_name: str,
    business_date: date | str,
    path_record: dict[str, Any] | None,
    weights: list[dict[str, Any]],
) -> dict[str, Any]:
    """把单笔事实按桥接权重展开到各父节点，并校验金额守恒。

    权重优先取桥接表的当日有效权重；没有桥接权重时退回层级路径上的直接
    父节点（权重 1）。尾差统一落到权重最大的父节点，保证分毫不差。
    """
    day = as_date(business_date)
    total = as_money(amount)
    ancestors = set(path_record.get("祖先路径编码") or []) if path_record else set()
    all_member_weights = [row for row in weights if str(row.get("成员编码", "")) == member]
    # 只有落在当日有效路径上的父节点才参与分摊：层级版本换了祖先，
    # 旧桥接权重不会把金额记到已经不在路径上的父节点。
    member_targets = [
        row for row in all_member_weights
        if not ancestors or str(row.get("父节点编码", "")) in ancestors
    ]
    off_path = [row for row in all_member_weights if row not in member_targets]
    member_targets, duplicates = _dedupe_targets(member_targets)

    note = ""
    if member_targets:
        source = "桥接权重"
        if off_path:
            note = "部分桥接父节点不在当日有效路径上，已跳过"
    elif path_record and path_record.get("父节点编码"):
        member_targets = [{
            "成员编码": member,
            "父节点编码": path_record["父节点编码"],
            "父节点名称": path_record.get("父节点名称", ""),
            "权重": "1",
        }]
        source = "层级直接父节点"
        if off_path:
            note = "桥接权重全部不在当日有效路径上，改按直接父节点分摊"
    else:
        raise BridgeAllocationError(
            f"事实 {fact_id} 的成员 {member} 在 {day.isoformat()} 既无桥接权重也无层级父节点"
        )

    raw_weights = [as_weight(row.get("权重", 0)) for row in member_targets]
    weight_sum = sum(raw_weights, Decimal("0")).quantize(WEIGHT)
    if weight_sum <= 0:
        raise BridgeAllocationError(f"事实 {fact_id} 的成员 {member} 桥接权重合计为 0")
    if weight_sum != Decimal("1.0000"):
        # 权重不归一时按比例归一，并在结果里标记，避免金额被放大或缩小。
        normalized = [w / weight_sum for w in raw_weights]
        weight_note = f"权重合计 {weight_sum}，已按比例归一"
    else:
        normalized = raw_weights
        weight_note = ""
    if note:
        weight_note = f"{weight_note}；{note}" if weight_note else note

    allocations: list[dict[str, Any]] = []
    allocated = Decimal("0")
    for index, (row, weight) in enumerate(zip(member_targets, normalized)):
        share = (total * weight).quantize(MONEY, rounding=ROUND_HALF_UP)
        allocations.append({
            "父节点编码": str(row.get("父节点编码", "")),
            "父节点名称": str(row.get("父节点名称", "") or row.get("父节点编码", "")),
            "分配权重": f"{weight.quantize(WEIGHT)}",
            "分摊金额": share,
            "_weight": weight,
            "_index": index,
        })
        allocated += share

    remainder = total - allocated
    if remainder != 0:
        target = max(allocations, key=lambda item: (item["_weight"], -item["_index"]))
        target["分摊金额"] = (target["分摊金额"] + remainder).quantize(MONEY)
        target["尾差调整"] = f"{remainder.quantize(MONEY)}"
        allocated = sum((item["分摊金额"] for item in allocations), Decimal("0"))

    if allocated != total:
        raise BridgeAllocationError(
            f"事实 {fact_id} 展开金额 {allocated} 与源金额 {total} 不相等"
        )

    for item in allocations:
        item.pop("_weight", None)
        item.pop("_index", None)

    return {
        "事实ID": fact_id,
        "成员编码": member,
        "源金额": f"{total}",
        "分摊合计": f"{allocated}",
        "金额守恒": allocated == total,
        "权重来源": source,
        "权重说明": weight_note,
        "重复路径": [
            {"成员编码": str(row.get("成员编码", "")), "父节点编码": str(row.get("父节点编码", "")),
             "权重": str(row.get("权重", ""))}
            for row in duplicates
        ],
        "路径外权重": [
            {"成员编码": str(row.get("成员编码", "")), "父节点编码": str(row.get("父节点编码", "")),
             "权重": str(row.get("权重", ""))}
            for row in off_path
        ],
        "分摊明细": allocations,
    }


def _source_facts(
    bridge_name: str,
    business_date: date | str | None,
    fact_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """取待分摊的源事实。

    业务日期与交付日期是两个维度：同一笔事实可以属于更晚的业务日期，
    但只在某个交付批次里交给桥接。这里只按“交付日期 = 复算日”取批次，
    业务日期留给路径解析逐笔判断，这样历史更正才有意义。
    """
    day = as_date(business_date) if business_date else None
    wanted = {str(f) for f in fact_ids} if fact_ids else None
    facts = []
    for row in store.rows(SOURCE_FACT_MODULE):
        if str(row.get("桥接名称", "")) != bridge_name:
            continue
        if wanted is not None and str(row.get("事实ID", "")) not in wanted:
            continue
        delivery = as_date(row.get("交付日期"), DATE_MIN)
        if day is not None and delivery != day:
            continue
        facts.append(deepcopy(row))
    return facts


def _active_details(bridge_name: str, fact_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    """取这些事实当前有效的分摊明细，用于识别历史更正。"""
    existing: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows(DETAIL_MODULE):
        if row.get("桥接名称") != bridge_name:
            continue
        if str(row.get("事实ID", "")) not in fact_ids:
            continue
        if row.get("状态") == "有效":
            existing.setdefault(str(row.get("事实ID", "")), []).append(deepcopy(row))
    return existing


def _supersede_active(bridge_name: str, fact_ids: set[str], batch: str, reason: str) -> list[str]:
    """同一笔事实复算时，旧的“有效”明细统一置为已替代。

    依事实ID而非业务日定位：一笔事实只应出现在一个版本的分摊里，
    重新展开后旧版本残留的明细不再计入事实分区。
    """
    superseded = []
    for row in store.rows(DETAIL_MODULE):
        if row.get("桥接名称") != bridge_name:
            continue
        if str(row.get("事实ID", "")) not in fact_ids:
            continue
        if row.get("状态") == "有效":
            row["状态"] = "已替代"
            row["被替代批次"] = batch
            row["替代原因"] = reason
            row["替代时间"] = _now_text()
            superseded.append(str(row.get("明细ID", "")))
    return superseded


def _allocation_snapshot(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把一组明细压缩成 父节点→金额/路径，用于比较更正前后差异。"""
    snapshot: dict[str, dict[str, Any]] = {}
    for row in rows:
        parent = str(row.get("父节点编码", ""))
        amount = Decimal(str(row.get("分摊金额", "0")))
        current = snapshot.setdefault(parent, {
            "父节点编码": parent,
            "父节点名称": row.get("父节点名称", ""),
            "分摊金额": Decimal("0"),
            "层级版本": row.get("层级版本", ""),
            "当日有效路径": row.get("当日有效路径", ""),
        })
        current["分摊金额"] += amount
        current["层级版本"] = row.get("层级版本", current["层级版本"])
        current["当日有效路径"] = row.get("当日有效路径", current["当日有效路径"])
    for item in snapshot.values():
        item["分摊金额"] = f"{Decimal(item['分摊金额']).quantize(MONEY)}"
    return snapshot


def _text_of(snapshot: dict[str, dict[str, Any]]) -> str:
    return "、".join(
        f"{item['父节点名称']}({item['父节点编码']}) {item['分摊金额']}"
        for item in snapshot.values()
    )


def _correction_record(
    fact_id: str,
    business_day: date,
    delivery_day: date,
    previous: dict[str, dict[str, Any]],
    current: dict[str, Any],
    business_path: str,
    delivery_path: str,
    version_label: str,
) -> dict[str, Any]:
    """登记一条历史更正：成员转父日期早于交付日期，当日应走的路径与原路径不同。"""
    current_snapshot = _allocation_snapshot_from_result(current)
    previous_version = next(iter(previous.values()))["层级版本"] if previous else "无"
    parts = []
    if previous:
        parts.append(f"原按 {previous_version} 分摊为 {_text_of(previous)}")
    else:
        parts.append("该事实首次交付时转父尚未生效")
    parts.append(f"按 {version_label} 的当日有效路径 {business_path} 分摊为 {_text_of(current_snapshot)}")
    if delivery_path and delivery_path != business_path:
        parts.append(f"交付日路径为 {delivery_path}，因业务日期早于转父日期而回退到当日有效路径")
    return {
        "事实ID": fact_id,
        "业务日期": business_day.isoformat(),
        "交付日期": delivery_day.isoformat(),
        "原层级版本": previous_version,
        "新层级版本": version_label,
        "原父节点分摊": previous,
        "新父节点分摊": current_snapshot,
        "当日有效路径": business_path,
        "交付日路径": delivery_path,
        "说明": "；".join(parts),
    }


def _allocation_snapshot_from_result(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for item in result["分摊明细"]:
        snapshot[item["父节点编码"]] = {
            "父节点编码": item["父节点编码"],
            "父节点名称": item["父节点名称"],
            "分摊金额": f"{item['分摊金额']}",
            "层级版本": result["层级版本"],
            "当日有效路径": result["当日有效路径"],
        }
    return snapshot


def publish_bridge_expansion(
    bridge: str | int,
    delivery_date: date | str,
    *,
    as_of: date | str | None = None,
    fact_ids: list[str] | None = None,
    reason: str = "日常分摊",
    version: dict[str, Any] | None = None,
    hierarchy: str | int | None = None,
) -> dict[str, Any]:
    """按层级构建交付的父子关系展开事实，登记父节点分摊结果。

    - ``delivery_date`` 圈定要复算的交付批次；
    - ``as_of`` 决定使用哪个层级版本（默认当天），也就是“用哪一版父子关系”；
    - 每笔事实再按自己的业务日期过滤该版本里的分段，得到当日有效路径。

    同一事实复算时旧明细置为已替代；若新旧父节点分摊不同，登记一条
    历史更正，说明转父日期早于交付日期时当日应走的路径。
    """
    delivery_day = as_date(delivery_date)
    as_of_day = as_date(as_of, today())
    bridge_entry = _bridge_name(bridge)
    bridge_name = str(bridge_entry.get("桥接名称", ""))
    hierarchy_name = resolve_hierarchy_reference(
        hierarchy or bridge_entry.get("层级名称") or bridge_entry.get("多对多维度", "")
    )
    chosen_version = version if version is not None else select_hierarchy_version(
        hierarchy_name, as_of=as_of_day
    )
    version_label = str((chosen_version or {}).get("层级版本", "实时"))

    facts = _source_facts(bridge_name, delivery_day, fact_ids)
    if not facts:
        raise BridgeAllocationError(
            f"桥接 {bridge_name} 在 {delivery_day.isoformat()} 没有可分摊的源事实"
        )

    batch = f"{bridge_name}-{delivery_day.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
    fact_keys = {str(row.get("事实ID", "")) for row in facts}
    previous_by_fact = _active_details(bridge_name, fact_keys)
    superseded = _supersede_active(bridge_name, fact_keys, batch, reason)

    path_cache: dict[date, dict[str, dict[str, Any]]] = {}
    details: list[dict[str, Any]] = []
    allocations: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []

    for fact in sorted(facts, key=lambda row: str(row.get("事实ID", ""))):
        fact_id = str(fact.get("事实ID", ""))
        member = str(fact.get("成员编码", ""))
        amount = as_money(fact.get("金额", 0))
        business_day = as_date(fact.get("业务日期"), delivery_day)

        if business_day not in path_cache:
            path_cache[business_day] = resolve_member_paths(
                hierarchy_name, business_day, version=chosen_version
            )
        if delivery_day not in path_cache:
            path_cache[delivery_day] = resolve_member_paths(
                hierarchy_name, delivery_day, version=chosen_version
            )
        path = path_cache[business_day].get(member)
        delivery_path = path_cache[delivery_day].get(member)
        delivery_parent = str(delivery_path.get("父节点编码", "")) if delivery_path else ""
        weights = _bridge_weights(bridge_name, business_day)
        result = allocate_fact_amount(
            fact_id=fact_id,
            member=member,
            amount=amount,
            bridge_name=bridge_name,
            business_date=business_day,
            path_record=path,
            weights=weights,
        )
        path_text = str(path.get("祖先路径文本", "")) if path else ""
        delivery_path_text = str(delivery_path.get("祖先路径文本", "")) if delivery_path else ""
        result["业务日期"] = business_day.isoformat()
        result["交付日期"] = delivery_day.isoformat()
        result["层级版本"] = version_label
        result["当日有效路径"] = path_text
        result["交付日路径"] = delivery_path_text
        result["父节点路径"] = [
            {"父节点编码": item["父节点编码"], "分摊金额": item["分摊金额"]}
            for item in result["分摊明细"]
        ]

        previous_rows = previous_by_fact.get(fact_id, [])
        previous_snapshot = _allocation_snapshot(previous_rows)
        current_snapshot = _allocation_snapshot_from_result(result)
        allocation_changed = (
            {key: item["分摊金额"] for key, item in previous_snapshot.items()}
            != {key: item["分摊金额"] for key, item in current_snapshot.items()}
        )
        # 转父日期早于交付日期时，业务日期在转父前的事实会走“当日有效路径”，
        # 与交付日视图不同，需要留下历史更正说明。
        path_moved = bool(delivery_parent) and delivery_parent != str(
            path.get("父节点编码", "") if path else ""
        )
        result["历史更正"] = path_moved or (bool(previous_rows) and allocation_changed)
        if result["历史更正"]:
            correction = _correction_record(
                fact_id, business_day, delivery_day,
                previous_snapshot, result, path_text, delivery_path_text, version_label,
            )
            result["更正说明"] = correction["说明"]
            corrections.append(correction)
        allocations.append(result)
        if not result["金额守恒"]:
            mismatches.append(result)

        for item in result["分摊明细"]:
            detail = {
                "id": _max_id(DETAIL_MODULE) + 1,
                "明细ID": f"{fact_id}-{member}-{item['父节点编码']}-{batch[-6:]}",
                "复算批次": batch,
                "桥接名称": bridge_name,
                "事实ID": fact_id,
                "成员编码": member,
                "成员名称": path.get("成员名称", "") if path else "",
                "父节点编码": item["父节点编码"],
                "父节点名称": item["父节点名称"],
                "层级名称": hierarchy_name,
                "层级版本": version_label,
                "历史更正": "是" if result["历史更正"] else "否",
                "版本记录ID": (chosen_version or {}).get("id"),
                "业务日期": business_day.isoformat(),
                "交付日期": delivery_day.isoformat(),
                "适用日期起": str(path.get("路径有效期起", "")) if path else "",
                "适用日期止": str(path.get("路径有效期止", "")) if path else "",
                "当日有效路径": path_text,
                "路径键": str(path.get("祖先路径键", "")) if path else "",
                "分配权重": item["分配权重"],
                "源金额": result["源金额"],
                "分摊金额": f"{item['分摊金额']}",
                "权重来源": result["权重来源"],
                "权重说明": result["权重说明"],
                "状态": "有效",
                "复算原因": reason,
                "生成时间": _now_text(),
            }
            details.append(detail)
            store.insert(DETAIL_MODULE, detail)

    if mismatches:
        raise BridgeAllocationError(
            f"桥接 {bridge_name} 存在展开金额与源事实不相等的记录："
            + "、".join(item["事实ID"] for item in mismatches)
        )

    partition = fact_partition_totals(details, allocations)
    store.channel.publish("bridge.expanded", {
        "bridge_name": bridge_name,
        "delivery_date": delivery_day.isoformat(),
        "as_of": as_of_day.isoformat(),
        "batch": batch,
        "fact_count": len(facts),
        "detail_count": len(details),
        "source_total": partition["源事实总额"],
        "allocated_total": partition["分摊总额"],
        "superseded_count": len(superseded),
        "hierarchy_version": version_label,
        "corrected_facts": len(corrections),
    })
    store.artifacts.register(
        "dim_bridge", "bridge_expansion",
        f"bridge_expansion_{batch}",
        {"bridge_name": bridge_name, "delivery_date": delivery_day.isoformat(),
         "source_total": partition["源事实总额"]},
        ["hierarchy_build", "dim_bridge", "bridge_source_fact"],
    )

    return {
        "桥接名称": bridge_name,
        "交付日期": delivery_day.isoformat(),
        "复算日期": as_of_day.isoformat(),
        "复算批次": batch,
        "层级版本": version_label,
        "事实笔数": len(facts),
        "明细笔数": len(details),
        "替代明细": superseded,
        "历史更正": corrections,
        "源事实总额": partition["源事实总额"],
        "分摊总额": partition["分摊总额"],
        "父节点分摊": partition["父节点分摊"],
        "版本分摊": partition["版本分摊"],
        "金额守恒": partition["金额守恒"],
        "分摊结果": allocations,
        "明细": details,
    }


def fact_partition_totals(
    details: list[dict[str, Any]],
    allocations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """汇总交付给事实分区的金额，同时校验源事实与分摊金额相等。

    源金额按“事实”去重后统计（一笔事实只有一份源金额），分摊金额按
    明细逐条累加；两者相等才说明展开没有多记或漏记。
    """
    if allocations is not None:
        source = sum((Decimal(str(row.get("源金额", "0"))) for row in allocations), Decimal("0"))
    else:
        seen: dict[str, Decimal] = {}
        for row in details:
            seen.setdefault(str(row.get("事实ID", "")), Decimal(str(row.get("源金额", "0"))))
        source = sum(seen.values(), Decimal("0"))
    allocated = sum((Decimal(str(row.get("分摊金额", "0"))) for row in details), Decimal("0"))
    source = source.quantize(MONEY)
    allocated = allocated.quantize(MONEY)
    by_parent: dict[str, Decimal] = {}
    by_version: dict[str, Decimal] = {}
    for row in details:
        parent = str(row.get("父节点编码", ""))
        by_parent[parent] = by_parent.get(parent, Decimal("0")) + Decimal(str(row.get("分摊金额", "0")))
        version = str(row.get("层级版本", ""))
        by_version[version] = by_version.get(version, Decimal("0")) + Decimal(str(row.get("分摊金额", "0")))
    return {
        "源事实总额": f"{source}",
        "分摊总额": f"{allocated}",
        "金额守恒": source == allocated,
        "父节点分摊": {key: f"{value.quantize(MONEY)}" for key, value in sorted(by_parent.items())},
        "版本分摊": {key: f"{value.quantize(MONEY)}" for key, value in sorted(by_version.items())},
    }
