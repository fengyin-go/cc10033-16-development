"""试跑：层级构建 → 桥接发布 → 事实分摊，覆盖月中更换分类的场景。

运行：
    cd backend && .venv/bin/python -m scripts.demo_midmonth_transfer

数据背景（见 app/seed.py）：
- 渠道分类层级里，成员 CH_JD 自 2026-11-15 起由“平台电商”转到“自营电商”；
- 层级版本 v1 构建于 2026-11-10（当时只有旧归属），v2 构建于 2026-11-16（含转父）；
  v1/v2 随种子数据预置，演示第 1 步会再构建一个 v3 验证接口；
- 交付批次 A（2026-11-10）含 3 笔事实，批次 B（2026-11-16）含 3 笔事实。
"""
from __future__ import annotations

from app.services.hierarchy_expansion import (
    build_hierarchy_snapshot,
    hierarchy_versions,
    validate_hierarchy,
    fact_partition_totals,
    member_path_records,
    publish_bridge_expansion,
    select_hierarchy_version,
)
from app.store import store

HIERARCHY = "渠道分类层级"
BRIDGE = "渠道分类分摊桥接"


def _show_paths(business_date: str) -> None:
    print(f"\n[成员路径] 业务日期 {business_date}")
    for row in member_path_records(HIERARCHY, business_date):
        if row["成员编码"] in {"CH_JD", "CH_TMALL"}:
            print(
                f"  {row['成员编码']:8s} 路径={row['祖先路径文本']:<20s}"
                f" 适用={row['路径有效期起']}~{row['路径有效期止']}"
            )


def _show_expansion(title: str, result: dict) -> None:
    print(f"\n[{title}] 交付日期 {result['交付日期']} / 复算日期 {result['复算日期']}")
    print(f"  使用层级版本：{result['层级版本']}")
    for fact in result["分摊结果"]:
        shares = "、".join(
            f"{row['父节点名称']}({row['父节点编码']}) {row['分摊金额']}"
            for row in fact["分摊明细"]
        )
        flag = " [历史更正]" if fact["历史更正"] else ""
        print(
            f"  {fact['事实ID']} 业务日 {fact['业务日期']} 源金额 {fact['源金额']:>9s}{flag}\n"
            f"      当日有效路径：{fact['当日有效路径']}\n"
            f"      父节点分摊：{shares}"
        )
    print(f"  源事实总额 {result['源事实总额']} / 分摊总额 {result['分摊总额']} / "
          f"守恒={result['金额守恒']}")
    print(f"  父节点合计：{result['父节点分摊']}")
    if result["历史更正"]:
        print("  历史更正说明：")
        for item in result["历史更正"]:
            print(f"    - {item['事实ID']}：{item['说明']}")


def main() -> None:
    print("=" * 78)
    print("步骤 1：层级构建 —— 校验父子关系并发布层级版本")
    print("=" * 78)
    versions = hierarchy_versions(HIERARCHY)
    for version in versions:
        print(f"  版本 {version['层级版本']} 构建日期 {version['构建日期']}，"
              f"成员边 {version['成员数']} 条")
    validation = validate_hierarchy(HIERARCHY)
    print(f"  父子关系校验通过：成员 {validation['成员数']} 个，"
          f"根节点 {validation['根节点']}")

    print("\n步骤 2：分类转移前后的当日有效路径")
    _show_paths("2026-11-14")
    _show_paths("2026-11-15")
    _show_paths("2026-11-20")

    print("\n" + "=" * 78)
    print("步骤 3：桥接发布 —— 交付批次 A 用 v1 展开")
    print("=" * 78)
    first = publish_bridge_expansion(BRIDGE, "2026-11-10", as_of="2026-11-10", reason="交付批次A")
    _show_expansion("批次 A", first)

    print("\n" + "=" * 78)
    print("步骤 4：月中转父后按 v2 复算同一交付批次（历史更正）")
    print("=" * 78)
    recalc = publish_bridge_expansion(BRIDGE, "2026-11-10", as_of="2026-11-20",
                                      reason="转父后复算")
    _show_expansion("批次 A 复算", recalc)
    print(f"  旧版本明细被替代 {len(recalc['替代明细'])} 条")

    print("\n" + "=" * 78)
    print("步骤 5：桥接发布 —— 交付批次 B 用 v2 展开")
    print("=" * 78)
    second = publish_bridge_expansion(BRIDGE, "2026-11-16", as_of="2026-11-16", reason="交付批次B")
    _show_expansion("批次 B", second)

    print("\n" + "=" * 78)
    print("步骤 6：交付给事实分区的总额（只统计状态=有效 的明细）")
    print("=" * 78)
    active = [
        row for row in store.rows("bridge_expansion_detail")
        if row["桥接名称"] == BRIDGE and row["状态"] == "有效"
    ]
    totals = fact_partition_totals(active)
    print(f"  有效明细 {len(active)} 条")
    print(f"  源事实总额 {totals['源事实总额']}")
    print(f"  分摊总额   {totals['分摊总额']}")
    print(f"  金额守恒   {totals['金额守恒']}")
    print(f"  按父节点   {totals['父节点分摊']}")
    print(f"  按层级版本 {totals['版本分摊']}")

    superseded = [
        row for row in store.rows("bridge_expansion_detail")
        if row["桥接名称"] == BRIDGE and row["状态"] == "已替代"
    ]
    print(f"  被替代明细 {len(superseded)} 条（不计入事实分区）")
    current_version = select_hierarchy_version(HIERARCHY, as_of="2026-11-20")
    print(f"  当前可用层级版本：{current_version['层级版本']}")

    print("\n" + "=" * 78)
    print("步骤 7：再执行一次层级构建，确认版本号向后追加")
    print("=" * 78)
    rebuilt = build_hierarchy_snapshot(HIERARCHY, built_on="2026-11-20", reason="重建层级")
    print(f"  构建日期 2026-11-20 → 版本 {rebuilt['层级版本']}，"
          f"成员边 {rebuilt['成员数']} 条，旧版本 v1/v2 仍可用于历史复算")


if __name__ == "__main__":
    main()
