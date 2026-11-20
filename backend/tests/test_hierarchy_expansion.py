"""层级展开与桥接分摊测试：循环校验、路径去重、金额守恒、版本与历史更正。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.seed import SEED_ROWS
from app.store import Store
from app.services import dim_bridge as dim_bridge_module
from app.services import hierarchy_build as hierarchy_build_module
from app.services import hierarchy_expansion as expansion


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    """每个测试用独立 store，并同步替换服务与路由模块里的全局引用。"""
    from app.routers import hierarchy as hierarchy_router

    fresh = Store()
    monkeypatch.setattr(expansion, "store", fresh)
    monkeypatch.setattr(dim_bridge_module, "store", fresh)
    monkeypatch.setattr(hierarchy_build_module, "store", fresh)
    monkeypatch.setattr(hierarchy_router, "store", fresh)
    return fresh


def _add_cycle(store: Store) -> None:
    """在 12 月起让 CH_TMALL 与 CH_APP 互相指向，形成 CH_TMALL → CH_APP → CH_TMALL。"""
    for row_id, member, parent, parent_name in (
        (99, "CH_TMALL", "CH_APP", "自营商城"),
        (100, "CH_APP", "CH_TMALL", "天猫渠道"),
    ):
        store.rows("hierarchy_member").append({
            "id": row_id, "层级名称": "渠道分类层级", "成员编码": member,
            "节点名称": {"CH_TMALL": "天猫渠道", "CH_APP": "自营商城"}[member],
            "父节点编码": parent, "父节点名称": parent_name,
            "生效日期": "2026-12-01", "失效日期": "9999-12-31", "录入日期": "2026-12-01",
            "status": "已生效", "pending": False, "abnormal": False,
        })


class TestHierarchyValidation:
    def test_valid_hierarchy_expands_paths(self, store: Store) -> None:
        result = expansion.validate_hierarchy("渠道分类层级")
        assert result["成员数"] == 8
        assert "CH_JD" in result["节点名称"]

    def test_cycle_error_names_members(self, store: Store) -> None:
        _add_cycle(store)
        with pytest.raises(expansion.HierarchyCycleError) as excinfo:
            expansion.validate_hierarchy("渠道分类层级")
        message = str(excinfo.value)
        assert "CH_TMALL" in message and "CH_APP" in message
        assert excinfo.value.cycles == [["CH_APP", "CH_TMALL", "CH_APP"]]

    def test_cycle_blocks_build_and_keeps_status(self, store: Store) -> None:
        _add_cycle(store)
        service = hierarchy_build_module.HierarchyBuildService()
        entry, message = service.run_action(3, "构建层级")
        assert entry is None
        assert "循环成员" in message
        row = store.find("hierarchy_build", 3)
        assert row["status"] == "草稿"
        assert row["abnormal"] is True

    def test_missing_member_data_is_reported(self, store: Store) -> None:
        store.insert("hierarchy_build", {
            "id": 50, "层级名称": "空层级", "所属维度": "DIM_STORE", "父子字段": "parent_id",
            "层级路径": "", "递归深度": "3", "桥接表": "", "扁平化": "否",
            "status": "草稿", "pending": True, "abnormal": False,
        })
        with pytest.raises(expansion.HierarchyError, match="缺少成员父子关系数据"):
            expansion.validate_hierarchy("空层级")


class TestPathExpansion:
    def test_path_uses_effective_segment(self, store: Store) -> None:
        before = expansion.resolve_member_paths("渠道分类层级", "2026-11-14")["CH_JD"]
        after = expansion.resolve_member_paths("渠道分类层级", "2026-11-15")["CH_JD"]
        assert before["祖先路径"] == ["线上渠道", "平台电商", "京东渠道"]
        assert after["祖先路径"] == ["线上渠道", "自营电商", "京东渠道"]
        assert before["适用日期"] == "2026-11-14"
        assert after["适用日期"] == "2026-11-15"

    def test_version_snapshot_keeps_old_parent(self, store: Store) -> None:
        v1 = expansion.select_hierarchy_version("渠道分类层级", as_of="2026-11-10")
        record = expansion.resolve_member_paths("渠道分类层级", "2026-11-20", version=v1)["CH_JD"]
        # v1 是转父前交付的版本，业务日期再晚也只能看到当时已知的父子关系。
        assert record["层级版本"] == "v1"
        assert record["父节点编码"] == "CH_PLATFORM"

    def test_shared_ancestor_path_not_duplicated(self, store: Store) -> None:
        records = expansion.member_path_records("渠道分类层级", "2026-11-10")
        ch_jd = next(row for row in records if row["成员编码"] == "CH_JD")
        ch_tmall = next(row for row in records if row["成员编码"] == "CH_TMALL")
        assert ch_jd["祖先路径"].count("线上渠道") == 1
        assert ch_tmall["祖先路径"].count("线上渠道") == 1


class TestBridgeAllocation:
    def test_duplicate_weight_path_not_double_counted(self, store: Store) -> None:
        result = expansion.publish_bridge_expansion(
            "渠道分类分摊桥接", "2026-11-10", as_of="2026-11-10"
        )
        fact = next(item for item in result["分摊结果"] if item["事实ID"] == "FACT-20261110-001")
        # 源事实 1000 元；重复权重 0.5 与 1.0 若都参与会得到 1500。
        assert Decimal(fact["分摊合计"]) == Decimal("1000.00")
        assert fact["重复路径"] == [{"成员编码": "CH_JD", "父节点编码": "CH_PLATFORM", "权重": "0.5"}]
        assert result["父节点分摊"]["CH_PLATFORM"] == "4060.50"

    def test_each_fact_expansion_equals_source(self, store: Store) -> None:
        result = expansion.publish_bridge_expansion("渠道分类分摊桥接", "2026-11-10", as_of="2026-11-20")
        for item in result["分摊结果"]:
            total = sum(Decimal(row["分摊金额"]) for row in item["分摊明细"])
            assert total == Decimal(item["源金额"])
            assert item["金额守恒"] is True

    def test_multi_parent_split_keeps_source_total(self, store: Store) -> None:
        result = expansion.publish_bridge_expansion(
            "渠道分类分摊桥接", "2026-11-10", as_of="2026-11-10"
        )
        fact = next(item for item in result["分摊结果"] if item["事实ID"] == "FACT-20261110-003")
        shares = {row["父节点编码"]: Decimal(row["分摊金额"]) for row in fact["分摊明细"]}
        assert shares == {"CH_PLATFORM": Decimal("560.00"), "CH_ONLINE": Decimal("240.00")}
        assert result["源事实总额"] == result["分摊总额"] == "4300.50"

    def test_rounding_remainder_lands_on_one_parent(self, store: Store) -> None:
        store.rows("bridge_source_fact").append({
            "id": 9, "事实ID": "FACT-ROUND-009", "桥接名称": "渠道分类分摊桥接",
            "成员编码": "CH_TMALL", "金额": "100.00", "业务日期": "2026-11-10",
            "交付日期": "2026-11-10", "status": "已交付", "pending": False, "abnormal": False,
        })
        result = expansion.publish_bridge_expansion(
            "渠道分类分摊桥接", "2026-11-10", as_of="2026-11-10", fact_ids=["FACT-ROUND-009"]
        )
        shares = [Decimal(row["分摊金额"]) for row in result["分摊结果"][0]["分摊明细"]]
        assert sum(shares) == Decimal("100.00")
        assert result["分摊结果"][0]["金额守恒"] is True


class TestVersionAndCorrection:
    def test_details_carry_version_path_and_date(self, store: Store) -> None:
        result = expansion.publish_bridge_expansion("渠道分类分摊桥接", "2026-11-10", as_of="2026-11-10")
        first = result["明细"][0]
        assert first["层级版本"] == "v1"
        assert first["业务日期"]
        assert first["适用日期起"] and first["适用日期止"]
        assert first["当日有效路径"].count("/") == 2

    def test_midmonth_transfer_uses_new_path_after_delivery(self, store: Store) -> None:
        result = expansion.publish_bridge_expansion("渠道分类分摊桥接", "2026-11-16", as_of="2026-11-16")
        fact = next(item for item in result["分摊结果"] if item["事实ID"] == "FACT-20261116-005")
        assert fact["层级版本"] == "v2"
        assert fact["当日有效路径"] == "线上渠道/自营电商/京东渠道"
        assert fact["分摊明细"][0]["父节点编码"] == "CH_SELF"
        assert fact["历史更正"] is False

    def test_delivery_batch_with_earlier_business_date_is_corrected(self, store: Store) -> None:
        result = expansion.publish_bridge_expansion("渠道分类分摊桥接", "2026-11-16", as_of="2026-11-16")
        fact = next(item for item in result["分摊结果"] if item["事实ID"] == "FACT-20261116-004")
        # 交付日路径是 v2 的自营电商，但业务日期 11-10 早于转父日，当日应走平台电商。
        assert fact["交付日路径"] == "线上渠道/自营电商/京东渠道"
        assert fact["当日有效路径"] == "线上渠道/平台电商/京东渠道"
        assert fact["历史更正"] is True
        assert fact["分摊明细"][0]["父节点编码"] == "CH_PLATFORM"

    def test_historical_correction_explains_effective_path(self, store: Store) -> None:
        expansion.publish_bridge_expansion("渠道分类分摊桥接", "2026-11-10", as_of="2026-11-10")
        result = expansion.publish_bridge_expansion(
            "渠道分类分摊桥接", "2026-11-10", as_of="2026-11-20", reason="转父后复算"
        )
        fact = next(item for item in result["分摊结果"] if item["事实ID"] == "FACT-20261110-002")
        # 原交付版本 v1 记在平台电商；转父后按 v2 的当日有效路径更正到自营电商。
        assert fact["层级版本"] == "v2"
        assert fact["历史更正"] is True
        assert fact["当日有效路径"] == "线上渠道/自营电商/京东渠道"
        assert fact["分摊明细"][0]["父节点编码"] == "CH_SELF"
        corrections = {item["事实ID"]: item for item in result["历史更正"]}
        correction = corrections["FACT-20261110-002"]
        assert correction["原层级版本"] == "v1"
        assert correction["新层级版本"] == "v2"
        assert correction["原父节点分摊"]["CH_PLATFORM"]["分摊金额"] == "2500.50"
        assert correction["新父节点分摊"]["CH_SELF"]["分摊金额"] == "2500.50"
        assert correction["当日有效路径"] == "线上渠道/自营电商/京东渠道"
        assert "业务日期早于转父日期" in correction["说明"]

    def test_fact_never_counted_in_two_versions(self, store: Store) -> None:
        expansion.publish_bridge_expansion("渠道分类分摊桥接", "2026-11-10", as_of="2026-11-10")
        first_active = [row for row in store.rows("bridge_expansion_detail") if row["状态"] == "有效"]
        expansion.publish_bridge_expansion("渠道分类分摊桥接", "2026-11-10", as_of="2026-11-20")
        second_active = [row for row in store.rows("bridge_expansion_detail") if row["状态"] == "有效"]
        superseded = [row for row in store.rows("bridge_expansion_detail") if row["状态"] == "已替代"]

        assert superseded, "复算后旧明细应被标记为已替代"
        assert {row["事实ID"] for row in first_active} == {row["事实ID"] for row in second_active}
        # 任一事实只出现在一个版本的有效明细里
        for fact_id in {row["事实ID"] for row in second_active}:
            versions = {row["层级版本"] for row in second_active if row["事实ID"] == fact_id}
            assert len(versions) == 1
        totals = expansion.fact_partition_totals(second_active)
        assert totals["金额守恒"] is True
        assert totals["源事实总额"] == "4300.50"


class TestServiceIntegration:
    def test_build_action_publishes_version_and_paths(self, store: Store) -> None:
        service = hierarchy_build_module.HierarchyBuildService()
        entry, message = service.run_action(3, "构建层级")
        assert entry is not None, message
        assert entry["status"] == "已构建"
        assert entry["层级版本"] == "v3"
        assert len(entry["_paths"]) == 8
        assert store.rows("hierarchy_version")[-1]["构建类型"] == "构建层级"

    def test_bridge_expand_facts_returns_parent_allocation(self, store: Store) -> None:
        service = dim_bridge_module.DimBridgeService()
        result, message = service.expand_facts(5, "2026-11-10", as_of="2026-11-10")
        assert result is not None, message
        assert "相等" in message
        assert result["父节点分摊"]["CH_PLATFORM"] == "4060.50"
        assert result["金额守恒"] is True

    def test_inactive_bridge_refuses_allocation(self, store: Store) -> None:
        service = dim_bridge_module.DimBridgeService()
        result, message = service.expand_facts(4, "2026-11-10")
        assert result is None
        assert "已失效" in message


class TestHierarchyRoutes:
    """路由层：错误要能带出循环成员，展开结果要能被查询到。"""

    def test_validation_route_returns_cycles(self, store: Store) -> None:
        from fastapi import HTTPException

        from app.routers.hierarchy import validate

        _add_cycle(store)
        with pytest.raises(HTTPException) as excinfo:
            validate("渠道分类层级", effective_on=None)
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail["循环成员"] == [["CH_APP", "CH_TMALL", "CH_APP"]]

    def test_paths_route_exposes_version_and_dates(self, store: Store) -> None:
        from app.routers.hierarchy import paths

        payload = paths("渠道分类层级", business_date="2026-11-15")
        record = next(item for item in payload["items"] if item["成员编码"] == "CH_JD")
        assert record["层级版本"] == "v1"  # 11-15 当天最新版本仍是 v1
        assert record["适用日期"] == "2026-11-15"

    def test_bridge_expansion_route_round_trip(self, store: Store) -> None:
        from app.routers.hierarchy import ExpandPayload, expand, expansion_details

        response = expand(
            5,
            ExpandPayload(delivery_date="2026-11-10", as_of="2026-11-20", reason="接口复算"),
        )
        assert response["ok"] is True
        assert response["result"]["金额守恒"] is True
        detail = expansion_details(5, delivery_date="2026-11-10", status="有效")
        assert detail["total"] > 0
        assert detail["totals"]["分摊总额"] == detail["totals"]["源事实总额"]
