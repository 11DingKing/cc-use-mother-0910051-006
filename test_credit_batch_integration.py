"""积分批次台账集成测试。

覆盖：
- 余额拆分为可追踪批次（来源年度/取得方式/剩余量/适用期限）
- 履约、出售、撤单、退回时的批次选择与恢复（可解释）
- 结转新批次保留来源链
- 过期处理不误伤预留给未完成交易的数量
- 规则换版后按原规则重放历史分配
- 全部分配与回退的数量守恒
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import crud, schemas, batch_service
from app.batch_rules import (
    BatchCandidate,
    select_batches,
    STRATEGY_EXPIRY_FIRST,
    STRATEGY_FIFO,
    STRATEGY_SOURCE_PRIORITY,
)
from app.models import (
    CreditRecordStatus,
    OrderType,
    OrderStatus,
    AcquisitionMethod,
    CreditBatchStatus,
    AllocationPurpose,
    AllocationStatus,
)

TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture(scope="function")
def db():
    engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def make_enterprise(db, name, short_name, credit_code):
    return crud.create_enterprise(
        db,
        schemas.EnterpriseCreate(name=name, short_name=short_name, credit_code=credit_code),
    )


def make_confirmed_record(db, enterprise_id, model_code, curb_weight, power_consumption,
                          annual_output, year, range_km=400.0):
    """录入车型并确认积分记录：正积分将触发批次核发"""
    model = crud.create_vehicle_model(
        db,
        schemas.VehicleModelCreate(
            enterprise_id=enterprise_id,
            model_name=f"车型{model_code}",
            model_code=model_code,
            curb_weight=curb_weight,
            power_consumption=power_consumption,
            range=range_km,
            annual_output=annual_output,
            production_year=year,
        ),
    )
    record = crud.create_credit_record(db, model.id, year)
    crud.update_credit_record_status(db, record.id, CreditRecordStatus.PUBLICIZED)
    crud.update_credit_record_status(db, record.id, CreditRecordStatus.CONFIRMED)
    return record


def make_positive_record(db, enterprise_id, model_code, year, annual_output=1000):
    """正积分车型：电耗低于限值。1000kg/8.0kWh → 单车0.5952分"""
    return make_confirmed_record(
        db, enterprise_id, model_code,
        curb_weight=1000.0, power_consumption=8.0,
        annual_output=annual_output, year=year,
    )


def make_negative_record(db, enterprise_id, model_code, year, annual_output=100):
    """负积分车型：电耗高于限值。1000kg/15.0kWh → 单车-1.0714分"""
    return make_confirmed_record(
        db, enterprise_id, model_code,
        curb_weight=1000.0, power_consumption=15.0,
        annual_output=annual_output, year=year,
    )


def batches_of(db, enterprise_id):
    return batch_service.get_batches(db, enterprise_id=enterprise_id, limit=1000)


def assert_conserved(db, enterprise_id=None):
    report = batch_service.check_conservation(db, enterprise_id)
    assert report["is_conserved"], f"数量守恒被破坏: {report['violations']}"
    return report


class TestBatchCreationAndBalance:
    """余额拆分：确认记录→核发批次；余额视图按来源拆分"""

    def test_confirmed_record_creates_issued_batch(self, db):
        ent = make_enterprise(db, "批次车企A", "批A", "BAT-A01")
        record = make_positive_record(db, ent.id, "BA-P1", year=2025, annual_output=1000)

        batches = batches_of(db, ent.id)
        assert len(batches) == 1
        batch = batches[0]
        assert batch.acquisition_method == AcquisitionMethod.ISSUED
        assert batch.source_year == 2025
        assert batch.acquired_year == 2025
        assert batch.initial_amount == round(record.total_credit, 2)
        assert batch.remaining_amount == round(record.total_credit, 2)
        assert batch.reserved_amount == 0.0
        assert batch.consumed_amount == 0.0
        assert batch.expired_amount == 0.0
        # 默认规则：核发批次3年有效（含取得年度）
        assert batch.expiry_year == 2025 + 3 - 1
        assert batch.status == CreditBatchStatus.ACTIVE
        assert batch.source_type == "credit_record"
        assert batch.source_id == record.id

        # 幂等：重复确认不会重复建批次
        again = batch_service.create_issued_batch_for_record(db, record)
        assert again.id == batch.id
        assert len(batches_of(db, ent.id)) == 1
        assert_conserved(db, ent.id)

    def test_negative_record_creates_no_batch(self, db):
        ent = make_enterprise(db, "批次车企B", "批B", "BAT-B01")
        make_negative_record(db, ent.id, "BB-N1", year=2025)
        assert batches_of(db, ent.id) == []
        assert_conserved(db, ent.id)

    def test_balance_view_splits_by_method_and_year(self, db):
        ent = make_enterprise(db, "批次车企C", "批C", "BAT-C01")
        r2024 = make_positive_record(db, ent.id, "BC-P24", year=2024, annual_output=100)
        r2025 = make_positive_record(db, ent.id, "BC-P25", year=2025, annual_output=200)

        balance = batch_service.get_batch_balance(db, ent.id)
        assert balance["batch_count"] == 2
        expected_total = round(r2024.total_credit + r2025.total_credit, 2)
        assert balance["total_initial"] == expected_total
        assert balance["total_remaining"] == expected_total
        assert balance["available_balance"] == expected_total

        issued_bucket = [b for b in balance["by_method"] if b["acquisition_method"] == "issued"]
        assert len(issued_bucket) == 1
        assert issued_bucket[0]["remaining"] == expected_total

        years = {b["source_year"]: b["remaining"] for b in balance["by_source_year"]}
        assert years[2024] == round(r2024.total_credit, 2)
        assert years[2025] == round(r2025.total_credit, 2)

        expiring = {e["expiry_year"]: e["remaining_at_risk"] for e in balance["expiring_by_year"]}
        assert expiring[2026] == round(r2024.total_credit, 2)
        assert expiring[2027] == round(r2025.total_credit, 2)
        assert_conserved(db, ent.id)


class TestComplianceFulfillment:
    """履约：按可解释规则选择批次抵偿缺口"""

    def test_fulfill_selects_earliest_expiry_first(self, db):
        ent = make_enterprise(db, "履约车企A", "履A", "COM-A01")
        # 三个不同到期年的核发批次（各59.52分）
        r23 = make_positive_record(db, ent.id, "CA-P23", year=2023, annual_output=100)  # 期限2025
        r24 = make_positive_record(db, ent.id, "CA-P24", year=2024, annual_output=100)  # 期限2026
        r25 = make_positive_record(db, ent.id, "CA-P25", year=2025, annual_output=100)  # 期限2027
        # 2025年负积分形成跨批次的缺口
        neg = make_negative_record(db, ent.id, "CA-N25", year=2025, annual_output=200)

        t23 = round(r23.total_credit, 2)
        t24 = round(r24.total_credit, 2)
        t25 = round(r25.total_credit, 2)
        summary = crud.update_annual_summary_with_transactions(db, ent.id, 2025)
        gap = round(summary.credit_gap, 2)
        assert gap == round(t25 - neg.total_credit * -1, 2) or gap > 0
        # 缺口应大于两个批次之和，确保跨三个批次
        assert t23 + t24 < gap < t23 + t24 + t25

        allocation = batch_service.fulfill_compliance(db, ent.id, 2025, gap)
        db.commit()

        assert allocation.purpose == AllocationPurpose.COMPLIANCE
        assert allocation.rule_version == "v1"
        assert allocation.total_amount == gap
        assert "先到期先核销" in allocation.explanation
        assert allocation.candidates_snapshot  # 候选快照已留存，可重放

        # 先到期先核销：2023批次（期限2025）先用完，再2024批次，最后2025批次
        b23 = [b for b in batches_of(db, ent.id) if b.source_year == 2023][0]
        b24 = [b for b in batches_of(db, ent.id) if b.source_year == 2024][0]
        b25 = [b for b in batches_of(db, ent.id) if b.source_year == 2025][0]
        assert len(allocation.items) == 3
        assert allocation.items[0].batch_id == b23.id
        assert allocation.items[0].amount == t23
        assert "到期" in allocation.items[0].reason
        assert allocation.items[1].batch_id == b24.id
        assert allocation.items[1].amount == t24
        assert allocation.items[2].batch_id == b25.id
        assert allocation.items[2].amount == round(gap - t23 - t24, 2)

        assert b23.remaining_amount == 0.0
        assert b23.consumed_amount == t23
        assert b24.remaining_amount == 0.0
        assert b25.remaining_amount == round(t25 - (gap - t23 - t24), 2)

        # 履约后缺口被抵偿，达标
        summary_after = crud.update_annual_summary_with_transactions(db, ent.id, 2025)
        fulfilled = batch_service.get_compliance_fulfilled_total(db, ent.id, 2025)
        assert fulfilled == gap
        assert round(summary_after.credit_gap - fulfilled, 2) <= 0
        assert summary_after.is_compliant is True
        assert_conserved(db, ent.id)

    def test_fulfill_insufficient_batches_raises(self, db):
        ent = make_enterprise(db, "履约车企B", "履B", "COM-B01")
        make_positive_record(db, ent.id, "CB-P25", year=2025, annual_output=10)
        make_negative_record(db, ent.id, "CB-N25", year=2025, annual_output=1000)

        summary = crud.update_annual_summary_with_transactions(db, ent.id, 2025)
        gap = round(summary.credit_gap, 2)
        with pytest.raises(ValueError, match="可用批次不足"):
            batch_service.fulfill_compliance(db, ent.id, 2025, gap)
        db.rollback()
        assert_conserved(db, ent.id)

    def test_fulfill_reverse_restores_batches(self, db):
        ent = make_enterprise(db, "履约车企C", "履C", "COM-C01")
        make_positive_record(db, ent.id, "CC-P25", year=2025, annual_output=500)
        make_negative_record(db, ent.id, "CC-N25", year=2025, annual_output=400)

        summary = crud.update_annual_summary_with_transactions(db, ent.id, 2025)
        gap = round(summary.credit_gap, 2)
        assert gap > 0

        allocation = batch_service.fulfill_compliance(db, ent.id, 2025, gap)
        db.commit()
        batch_before = batches_of(db, ent.id)[0]
        assert batch_before.consumed_amount == gap

        created = batch_service.reverse_allocation(db, allocation.id, "履约复核退回")
        db.commit()

        batch_after = batches_of(db, ent.id)[0]
        assert batch_after.consumed_amount == 0.0
        assert batch_after.remaining_amount == batch_after.initial_amount
        assert allocation.status == AllocationStatus.REVERSED
        assert any(a.purpose == AllocationPurpose.RETURN for a in created)
        assert "退回" in created[0].explanation

        # 回退后履约量归零，缺口恢复
        fulfilled = batch_service.get_compliance_fulfilled_total(db, ent.id, 2025)
        assert fulfilled == 0.0
        # 已回退的分配不能再次回退
        with pytest.raises(ValueError, match="已回退"):
            batch_service.reverse_allocation(db, allocation.id)
        db.rollback()
        assert_conserved(db, ent.id)


class TestSaleReserveConsumeCancel:
    """出售全链路：挂单预留→成交核销→撤单释放；买方批次继承来源"""

    def _setup_seller(self, db, output=1000):
        seller = make_enterprise(db, "出售方", "卖方", "SELL-01")
        record = make_positive_record(db, seller.id, "SELL-P25", year=2025, annual_output=output)
        return seller, record

    def test_order_lifecycle_with_batches(self, db):
        seller, record = self._setup_seller(db)
        buyer = make_enterprise(db, "购买方", "买方", "BUY-01")
        total = round(record.total_credit, 2)

        # 1) 挂单：批次预留
        sell_order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000.0, total_amount=round(total * 0.4, 2),
        ))
        batch = batches_of(db, seller.id)[0]
        assert batch.reserved_amount == round(total * 0.4, 2)
        assert batch.remaining_amount == round(total - total * 0.4, 2)
        assert_conserved(db, seller.id)

        # 预留后可用额下降，超额挂单被拒绝
        with pytest.raises(ValueError):
            crud.create_credit_order(db, schemas.CreditOrderCreate(
                enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
                unit_price=3000.0, total_amount=total,
            ))
        db.rollback()

        # 2) 部分成交：预留核销，买方获得购入批次（继承来源年度与期限）
        buy_order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2025, order_type=OrderType.BUY,
            unit_price=3500.0, total_amount=round(total * 0.25, 2),
        ))
        txn, error = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell_order.id, buy_order_id=buy_order.id,
            credit_amount=round(total * 0.25, 2),
        ))
        assert error is None
        db.expire_all()

        batch = batches_of(db, seller.id)[0]
        assert batch.consumed_amount == round(total * 0.25, 2)
        assert batch.reserved_amount == round(total * 0.4 - total * 0.25, 2)

        buyer_batches = batches_of(db, buyer.id)
        assert len(buyer_batches) == 1
        purchased = buyer_batches[0]
        assert purchased.acquisition_method == AcquisitionMethod.PURCHASED
        assert purchased.source_year == 2025  # 继承卖方批次来源年度
        assert purchased.expiry_year == batch.expiry_year  # 继承适用期限
        assert purchased.initial_amount == round(total * 0.25, 2)
        assert purchased.remaining_amount == round(total * 0.25, 2)

        # 来源链：买方批次 → 卖方批次
        lineage = batch_service.get_batch_lineage(db, purchased.id)
        assert lineage["acquisition_method"] == "purchased"
        assert len(lineage["parents"]) == 1
        assert lineage["parents"][0]["batch_id"] == batch.id
        assert lineage["parents"][0]["acquisition_method"] == "issued"

        # 出售核销台账可解释
        sale_alloc = db.query(type(txn)).filter_by(id=txn.id).first()
        from app import models as m
        sale_allocation = db.query(m.BatchAllocation).filter_by(
            reference_type="credit_transaction", reference_id=txn.id,
            purpose=AllocationPurpose.SALE,
        ).first()
        assert sale_allocation is not None
        assert "预留" in sale_allocation.explanation

        # 3) 撤单：剩余预留原路释放
        crud.cancel_credit_order(db, sell_order.id)
        db.expire_all()
        batch = batches_of(db, seller.id)[0]
        assert batch.reserved_amount == 0.0
        assert batch.remaining_amount == round(total - total * 0.25, 2)
        assert batch.consumed_amount == round(total * 0.25, 2)

        release_allocation = db.query(m.BatchAllocation).filter_by(
            reference_type="credit_order", reference_id=sell_order.id,
            purpose=AllocationPurpose.RELEASE,
        ).first()
        assert release_allocation is not None
        assert "撤单" in release_allocation.explanation

        assert_conserved(db, seller.id)
        assert_conserved(db, buyer.id)

    def test_transaction_return_restores_both_sides(self, db):
        seller, record = self._setup_seller(db)
        buyer = make_enterprise(db, "退回买方", "退买", "BUY-02")
        total = round(record.total_credit, 2)

        sell_order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000.0, total_amount=round(total * 0.5, 2),
        ))
        buy_order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2025, order_type=OrderType.BUY,
            unit_price=3500.0, total_amount=round(total * 0.5, 2),
        ))
        txn, error = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell_order.id, buy_order_id=buy_order.id,
            credit_amount=round(total * 0.5, 2),
        ))
        assert error is None
        db.expire_all()

        # 交易退回：卖方恢复、买方追回（订单已全额成交，无预留剩余）
        created = batch_service.return_transaction(db, txn.id, "交易复核退回")
        db.commit()
        db.expire_all()

        seller_batch = batches_of(db, seller.id)[0]
        assert seller_batch.consumed_amount == 0.0
        assert seller_batch.remaining_amount == total
        assert seller_batch.reserved_amount == 0.0

        purchased = batches_of(db, buyer.id)[0]
        assert purchased.initial_amount == 0.0
        assert purchased.remaining_amount == 0.0

        from app import models as m
        db.refresh(txn)
        assert txn.status == "returned"
        purposes = {a.purpose for a in created}
        assert AllocationPurpose.RETURN in purposes
        assert AllocationPurpose.CLAWBACK in purposes

        # 退回后交易不再计入成交口径（按交易发生年度核对）
        txn_year = txn.transaction_date.year
        summary = crud.update_annual_summary_with_transactions(db, seller.id, txn_year)
        assert summary.sold_credit == 0.0
        assert_conserved(db, seller.id)
        assert_conserved(db, buyer.id)

    def test_return_rejected_when_buyer_spent(self, db):
        seller, record = self._setup_seller(db)
        buyer = make_enterprise(db, "已用买方", "已用", "BUY-03")
        total = round(record.total_credit, 2)

        sell_order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000.0, total_amount=round(total * 0.5, 2),
        ))
        buy_order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2025, order_type=OrderType.BUY,
            unit_price=3500.0, total_amount=round(total * 0.5, 2),
        ))
        txn, error = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell_order.id, buy_order_id=buy_order.id,
            credit_amount=round(total * 0.5, 2),
        ))
        assert error is None
        db.expire_all()

        # 买方把购入积分履约用掉
        make_negative_record(db, buyer.id, "BUY-N25", year=2025, annual_output=100)
        purchased = batches_of(db, buyer.id)[0]
        batch_service.consume_compliance_from_batch(
            db, purchased, purchased.remaining_amount, 2025,
            reference_type="compliance", reference_id=None,
        )
        db.commit()

        with pytest.raises(ValueError, match="已被使用"):
            batch_service.return_transaction(db, txn.id)
        db.rollback()
        assert_conserved(db, seller.id)
        assert_conserved(db, buyer.id)


class TestCarryoverLineage:
    """结转：新批次保留来源链"""

    def test_carryover_batch_keeps_source_chain(self, db):
        ent = make_enterprise(db, "结转车企A", "结A", "CAR-A01")
        r2024 = make_positive_record(db, ent.id, "CRA-P24", year=2024, annual_output=1000)
        total = round(r2024.total_credit, 2)

        carryover = crud.create_credit_carryover(db, schemas.CreditCarryoverCreate(
            enterprise_id=ent.id, from_year=2024, to_year=2025,
            original_amount=total, carryover_ratio=0.8,
            carryover_amount=round(total * 0.8, 2),
            remark="测试结转",
        ))
        db.expire_all()

        source_batch = [b for b in batches_of(db, ent.id)
                        if b.acquisition_method == AcquisitionMethod.ISSUED][0]
        carry_batch = [b for b in batches_of(db, ent.id)
                       if b.acquisition_method == AcquisitionMethod.CARRYOVER][0]

        # 来源批次转出消耗，新批次入账
        assert source_batch.consumed_amount == round(total * 0.8, 2)
        assert source_batch.remaining_amount == round(total * 0.2, 2)
        assert carry_batch.initial_amount == round(total * 0.8, 2)
        assert carry_batch.source_year == 2024
        assert carry_batch.acquired_year == 2025
        assert carry_batch.expiry_year == 2025 + 2 - 1  # 结转批次2年有效

        # 来源链：结转批次 → 核发批次
        lineage = batch_service.get_batch_lineage(db, carry_batch.id)
        assert lineage["acquisition_method"] == "carryover"
        assert len(lineage["parents"]) == 1
        parent = lineage["parents"][0]
        assert parent["batch_id"] == source_batch.id
        assert parent["acquisition_method"] == "issued"
        assert parent["transferred_amount"] == round(total * 0.8, 2)

        # 结转账目数量守恒：转出消耗 = 新批次初始量
        assert round(source_batch.consumed_amount, 2) == round(carry_batch.initial_amount, 2)
        assert_conserved(db, ent.id)

    def test_chained_carryover_lineage_reaches_issued_root(self, db):
        ent = make_enterprise(db, "结转车企B", "结B", "CAR-B01")
        r2023 = make_positive_record(db, ent.id, "CRB-P23", year=2023, annual_output=1000)
        total = round(r2023.total_credit, 2)

        c1 = crud.create_credit_carryover(db, schemas.CreditCarryoverCreate(
            enterprise_id=ent.id, from_year=2023, to_year=2024,
            original_amount=total, carryover_ratio=0.8,
            carryover_amount=round(total * 0.8, 2),
        ))
        c2_amount = round(total * 0.8 * 0.8, 2)
        c2 = crud.create_credit_carryover(db, schemas.CreditCarryoverCreate(
            enterprise_id=ent.id, from_year=2024, to_year=2025,
            original_amount=round(total * 0.8, 2), carryover_ratio=0.8,
            carryover_amount=c2_amount,
        ))
        db.expire_all()

        batch2 = [b for b in batches_of(db, ent.id)
                  if b.source_type == "credit_carryover" and b.source_id == c2.id][0]
        lineage = batch_service.get_batch_lineage(db, batch2.id)
        # 二级结转批次的来源链同时挂在核发批次与一级结转批次上（先到期先转出）
        assert lineage["acquired_year"] == 2025
        parents_by_method = {p["acquisition_method"]: p for p in lineage["parents"]}
        assert "issued" in parents_by_method
        assert "carryover" in parents_by_method
        carry_parent = parents_by_method["carryover"]
        assert carry_parent["acquired_year"] == 2024
        # 一级结转批次的父批次是最初的核发批次
        grand = carry_parent["parents"][0]
        assert grand["acquisition_method"] == "issued"
        assert grand["source_year"] == 2023
        assert_conserved(db, ent.id)


class TestExpiryProtection:
    """过期处理：不误伤预留给未完成交易的数量"""

    def test_expire_skips_reserved_amount(self, db):
        ent = make_enterprise(db, "过期车企A", "过A", "EXP-A01")
        old = make_positive_record(db, ent.id, "EA-P22", year=2022, annual_output=100)  # 期限2024
        new = make_positive_record(db, ent.id, "EA-P25", year=2025, annual_output=100)  # 期限2027
        old_total = round(old.total_credit, 2)

        # 挂单预留一部分（先到期先预留，锁定2022批次）
        reserve_amount = round(old_total * 0.3, 2)
        order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000.0, total_amount=reserve_amount,
        ))
        db.expire_all()
        old_batch = [b for b in batches_of(db, ent.id) if b.source_year == 2022][0]
        assert old_batch.reserved_amount == reserve_amount

        # 过期处理：2022批次未预留部分核销，预留部分保留
        allocations = batch_service.expire_batches(db, as_of_year=2025)
        db.commit()
        db.expire_all()

        old_batch = [b for b in batches_of(db, ent.id) if b.source_year == 2022][0]
        assert old_batch.expired_amount == round(old_total - reserve_amount, 2)
        assert old_batch.reserved_amount == reserve_amount  # 预留未被误伤
        assert old_batch.remaining_amount == 0.0
        assert old_batch.status == CreditBatchStatus.ACTIVE  # 仍有预留，未整体过期

        expire_allocation = allocations[0]
        assert expire_allocation.purpose == AllocationPurpose.EXPIRE
        assert "预留" in expire_allocation.explanation
        item = expire_allocation.items[0]
        assert item.amount == round(old_total - reserve_amount, 2)
        assert "保留" in item.reason

        new_batch = [b for b in batches_of(db, ent.id) if b.source_year == 2025][0]
        assert new_batch.expired_amount == 0.0  # 未到期批次不受影响
        assert_conserved(db, ent.id)

        # 撤单：释放量落在已过期批次上，直接核销而非回到可用池
        crud.cancel_credit_order(db, order.id)
        db.expire_all()
        old_batch = [b for b in batches_of(db, ent.id) if b.source_year == 2022][0]
        assert old_batch.reserved_amount == 0.0
        assert old_batch.remaining_amount == 0.0
        assert old_batch.expired_amount == old_total  # 全部过期
        assert old_batch.status == CreditBatchStatus.EXPIRED
        assert_conserved(db, ent.id)

    def test_expire_only_due_batches(self, db):
        ent = make_enterprise(db, "过期车企B", "过B", "EXP-B01")
        make_positive_record(db, ent.id, "EB-P25", year=2025, annual_output=100)
        allocations = batch_service.expire_batches(db, as_of_year=2025)
        db.commit()
        assert allocations == []  # 2025批次期限至2027，不处理
        batch = batches_of(db, ent.id)[0]
        assert batch.expired_amount == 0.0
        assert_conserved(db, ent.id)


class TestRuleVersioningAndReplay:
    """规则换版：历史分配按原规则重放"""

    def test_pure_selection_strategies_are_deterministic(self):
        candidates = [
            BatchCandidate(batch_id=1, batch_no="B1", available=100.0,
                           acquisition_method="issued", source_year=2025,
                           acquired_year=2025, expiry_year=2027),
            BatchCandidate(batch_id=2, batch_no="B2", available=100.0,
                           acquisition_method="purchased", source_year=2022,
                           acquired_year=2026, expiry_year=2024),
        ]
        expiry_first = select_batches(candidates, 100.0, strategy=STRATEGY_EXPIRY_FIRST,
                                      purpose="compliance", rule_version="v1")
        fifo = select_batches(candidates, 100.0, strategy=STRATEGY_FIFO,
                              purpose="compliance", rule_version="v2")
        by_source = select_batches(candidates, 100.0, strategy=STRATEGY_SOURCE_PRIORITY,
                                   source_priority=["issued", "purchased"],
                                   purpose="compliance", rule_version="v3")
        # 先到期：购入批次（2024到期）优先；先取得：核发批次（2025取得）优先
        assert expiry_first.items[0].batch_id == 2
        assert fifo.items[0].batch_id == 1
        assert by_source.items[0].batch_id == 1
        # 确定性：同输入重放结果一致
        again = select_batches(candidates, 100.0, strategy=STRATEGY_EXPIRY_FIRST,
                               purpose="compliance", rule_version="v1")
        assert [(i.batch_id, i.amount) for i in again.items] == \
               [(i.batch_id, i.amount) for i in expiry_first.items]

    def test_replay_after_rule_upgrade(self, db):
        ent = make_enterprise(db, "重放车企A", "重A", "RPL-A01")
        seller = make_enterprise(db, "重放卖方", "重卖", "RPL-S01")
        # 卖方2022年积分（期限2024）卖给本企业 → 购入批次取得晚但到期早
        make_positive_record(db, seller.id, "RS-P22", year=2022, annual_output=200)
        make_positive_record(db, ent.id, "RA-P25", year=2025, annual_output=100)  # 期限2027
        make_negative_record(db, ent.id, "RA-N25", year=2025, annual_output=100)

        seller_batch = batches_of(db, seller.id)[0]
        txn = crud.create_credit_transaction(db, schemas.CreditTransactionCreate(
            from_enterprise_id=seller.id, to_enterprise_id=ent.id,
            credit_amount=round(seller_batch.remaining_amount, 2),
            unit_price=3000.0,
        ))
        db.expire_all()

        # v1（先到期先核销）下履约：应选购入批次（期限2024最早）
        summary = crud.update_annual_summary_with_transactions(db, ent.id, 2025)
        gap = round(summary.credit_gap, 2)
        allocation = batch_service.fulfill_compliance(db, ent.id, 2025, gap)
        db.commit()
        purchased = [b for b in batches_of(db, ent.id)
                     if b.acquisition_method == AcquisitionMethod.PURCHASED][0]
        assert allocation.items[0].batch_id == purchased.id
        assert allocation.rule_version == "v1"

        # 换版：启用 fifo 的 v2
        batch_service.create_rule_version(
            db, version="v2", name="先取得先核销", strategy=STRATEGY_FIFO,
            params={}, activate=True,
        )
        db.commit()

        # 按原规则重放：与历史一致
        replay_original = batch_service.replay_allocation(db, allocation.id)
        assert replay_original["original_rule_version"] == "v1"
        assert replay_original["replay_rule_version"] == "v1"
        assert replay_original["matches"] is True

        # 按新版规则重放：选择不同（先取得先核销 → 2025核发批次优先）
        replay_v2 = batch_service.replay_allocation(db, allocation.id, rule_version="v2")
        assert replay_v2["replay_rule_version"] == "v2"
        assert replay_v2["matches"] is False
        issued = [b for b in batches_of(db, ent.id)
                  if b.acquisition_method == AcquisitionMethod.ISSUED][0]
        assert replay_v2["replayed_items"][0]["batch_id"] == issued.id

        # 历史分配记录本身不被换版改变
        db.refresh(allocation)
        assert allocation.rule_version == "v1"
        assert allocation.items[0].batch_id == purchased.id
        assert_conserved(db, ent.id)
        assert_conserved(db, seller.id)

    def test_new_allocations_use_active_rule(self, db):
        ent = make_enterprise(db, "重放车企B", "重B", "RPL-B01")
        make_positive_record(db, ent.id, "RB-P25", year=2025, annual_output=100)
        make_negative_record(db, ent.id, "RB-N25", year=2025, annual_output=110)

        batch_service.create_rule_version(
            db, version="v9", name="来源优先级", strategy=STRATEGY_SOURCE_PRIORITY,
            params={"source_priority": ["carryover", "purchased", "issued"]},
            activate=True,
        )
        db.commit()

        summary = crud.update_annual_summary_with_transactions(db, ent.id, 2025)
        allocation = batch_service.fulfill_compliance(
            db, ent.id, 2025, round(summary.credit_gap, 2))
        db.commit()
        assert allocation.rule_version == "v9"
        assert "优先级" in allocation.explanation

        replay = batch_service.replay_allocation(db, allocation.id)
        assert replay["matches"] is True
        assert_conserved(db, ent.id)


class TestConservationAcrossFlows:
    """数量守恒：分配、回退、结转、过期全链路"""

    def test_full_lifecycle_conservation(self, db):
        from datetime import datetime as dt
        now_year = dt.now().year
        seller = make_enterprise(db, "守恒卖方", "守卖", "CON-S01")
        buyer = make_enterprise(db, "守恒买方", "守买", "CON-B01")

        r_s = make_positive_record(db, seller.id, "CS-P25", year=2025, annual_output=1000)
        # 买方缺口放在交易发生年度，使购入积分计入该年度口径
        make_negative_record(db, buyer.id, "CB-N25", year=now_year, annual_output=200)
        total_s = round(r_s.total_credit, 2)

        # 卖方挂单并部分成交
        sell_order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000.0, total_amount=round(total_s * 0.6, 2),
        ))
        buy_order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2025, order_type=OrderType.BUY,
            unit_price=3500.0, total_amount=round(total_s * 0.3, 2),
        ))
        txn, error = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell_order.id, buy_order_id=buy_order.id,
            credit_amount=round(total_s * 0.3, 2),
        ))
        assert error is None

        # 卖方结转一部分到2026
        carry_amount = round(total_s * 0.2, 2)
        crud.create_credit_carryover(db, schemas.CreditCarryoverCreate(
            enterprise_id=seller.id, from_year=2025, to_year=2026,
            original_amount=total_s, carryover_ratio=1.0,
            carryover_amount=carry_amount,
        ))

        # 买方履约
        summary = crud.update_annual_summary_with_transactions(db, buyer.id, now_year)
        gap = round(summary.credit_gap, 2)
        if gap > 0.01:
            batch_service.fulfill_compliance(db, buyer.id, now_year, gap)
            db.commit()

        # 卖方撤单释放剩余预留
        crud.cancel_credit_order(db, sell_order.id)

        # 全局守恒：每笔出售核销都有对应的购入入账
        from app import models as m
        sale_total = db.query(m.BatchAllocation).filter_by(
            purpose=AllocationPurpose.SALE).all()
        purchase_total = db.query(m.BatchAllocation).filter_by(
            purpose=AllocationPurpose.PURCHASE).all()
        assert round(sum(a.total_amount for a in sale_total), 2) == \
               round(sum(a.total_amount for a in purchase_total), 2)

        assert_conserved(db, seller.id)
        assert_conserved(db, buyer.id)
        assert_conserved(db)  # 全局

        # 台账重算与当前状态一致（check_conservation 已含），再独立抽验一个批次
        batches = batches_of(db, seller.id) + batches_of(db, buyer.id)
        for batch in batches:
            parts = round(batch.remaining_amount + batch.reserved_amount
                          + batch.consumed_amount + batch.expired_amount, 2)
            assert abs(parts - round(batch.initial_amount, 2)) <= 0.02

    def test_available_balance_caps_traditional_match(self, db):
        """传统撮合的可转让量受批次可用余额约束"""
        seller = make_enterprise(db, "限流卖方", "限卖", "CAP-S01")
        deficit = make_enterprise(db, "限流买方", "限买", "CAP-B01")
        r_s = make_positive_record(db, seller.id, "CS2-P25", year=2025, annual_output=1000)
        make_negative_record(db, deficit.id, "CB2-N25", year=2025, annual_output=5000)
        total_s = round(r_s.total_credit, 2)

        # 先挂单锁定 80%
        order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000.0, total_amount=round(total_s * 0.8, 2),
        ))
        db.expire_all()

        # 传统撮合只能动用剩余 20% 可用余额
        transactions, remaining_gap, remaining_surplus = crud.match_and_execute_transactions(
            db, year=2025, unit_price=3000.0
        )
        transferred = round(sum(t.credit_amount for t in transactions), 2)
        assert transferred == round(total_s * 0.2, 2)
        assert_conserved(db, seller.id)
        assert_conserved(db, deficit.id)


class TestBatchApi:
    """接口层：余额拆分、履约、解释、重放、守恒"""

    @pytest.fixture()
    def client(self, db):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.database import get_db

        def override_get_db():
            try:
                yield db
            finally:
                pass

        app.dependency_overrides[get_db] = override_get_db
        try:
            with TestClient(app) as test_client:
                yield test_client
        finally:
            app.dependency_overrides.clear()

    def test_batch_api_end_to_end(self, db, client):
        ent = make_enterprise(db, "接口车企A", "接A", "API-A01")
        make_positive_record(db, ent.id, "AA-P24", year=2024, annual_output=100)
        make_positive_record(db, ent.id, "AA-P25", year=2025, annual_output=100)
        make_negative_record(db, ent.id, "AA-N25", year=2025, annual_output=80)

        prefix = "/api/v1/credit-batches"

        # 余额拆分视图
        resp = client.get(f"{prefix}/balance/{ent.id}")
        assert resp.status_code == 200
        balance = resp.json()
        assert balance["batch_count"] == 2
        assert balance["total_remaining"] > 0

        # 履约（接口层自动按缺口全额）
        resp = client.post(f"{prefix}/compliance/fulfill",
                           json={"enterprise_id": ent.id, "year": 2025})
        assert resp.status_code == 200
        allocation = resp.json()
        assert allocation["purpose"] == "compliance"
        assert allocation["rule_version"] == "v1"
        assert allocation["explanation"]
        assert len(allocation["items"]) >= 1
        assert allocation["items"][0]["reason"]

        # 分配详情：为何选择这些批次
        resp = client.get(f"{prefix}/allocations/{allocation['id']}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["items"][0]["batch_no"]

        # 重放：按原规则复算一致
        resp = client.post(f"{prefix}/allocations/{allocation['id']}/replay", json={})
        assert resp.status_code == 200
        assert resp.json()["matches"] is True

        # 批次来源链
        resp = client.get(f"{prefix}/batches")
        assert resp.status_code == 200
        batch_id = resp.json()[0]["id"]
        resp = client.get(f"{prefix}/batches/{batch_id}/lineage")
        assert resp.status_code == 200
        assert resp.json()["batch_id"] == batch_id

        # 守恒校验
        resp = client.get(f"{prefix}/conservation/{ent.id}")
        assert resp.status_code == 200
        assert resp.json()["is_conserved"] is True

        # 履约退回
        resp = client.post(f"{prefix}/allocations/{allocation['id']}/reverse",
                           json={"reason": "接口测试退回"})
        assert resp.status_code == 200
        assert resp.json()[0]["purpose"] == "return"
        resp = client.get(f"{prefix}/conservation/{ent.id}")
        assert resp.json()["is_conserved"] is True

        # 规则版本列表与新版
        resp = client.get(f"{prefix}/rules")
        assert resp.status_code == 200
        assert any(r["version"] == "v1" for r in resp.json())
        resp = client.post(f"{prefix}/rules", json={
            "version": "v2", "name": "先取得先核销", "strategy": "fifo",
        })
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

        # 过期处理接口
        resp = client.post(f"{prefix}/expire", json={"as_of_year": 2030})
        assert resp.status_code == 200
        assert resp.json()["total_expired"] > 0
        resp = client.get(f"{prefix}/conservation/{ent.id}")
        assert resp.json()["is_conserved"] is True
