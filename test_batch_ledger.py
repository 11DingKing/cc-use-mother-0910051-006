"""积分批次台账测试：选批、预留/成交/撤单/退回、结转链、过期保护、重放、守恒。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import crud, schemas, batch_service
from app.batch_rules import select_batches, BatchCandidate
from app.models import (
    CreditRecordStatus, OrderType,
    BatchAcquisitionMethod, BatchStatus,
    AllocationPurpose, AllocationStatus,
    CreditBatch, CreditBatchAllocation,
)


@pytest.fixture(scope="function")
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Testing = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Testing()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _enterprise(db, name, code):
    return crud.create_enterprise(
        db, schemas.EnterpriseCreate(name=name, short_name=name, credit_code=code)
    )


def _positive_model(db, ent, year, code, output=1000, pc=8.0, weight=1000.0):
    m = crud.create_vehicle_model(db, schemas.VehicleModelCreate(
        enterprise_id=ent.id, model_name=code, model_code=code,
        curb_weight=weight, power_consumption=pc, range=500.0,
        annual_output=output, production_year=year,
    ))
    rec = crud.create_credit_record(db, m.id, year)
    crud.update_credit_record_status(db, rec.id, CreditRecordStatus.PUBLICIZED)
    crud.update_credit_record_status(db, rec.id, CreditRecordStatus.CONFIRMED)
    return rec


# ---------------------------------------------------------------------------
# 选批规则引擎
# ---------------------------------------------------------------------------

class TestBatchRules:

    def _cand(self, bid, year, method, remaining, until):
        return BatchCandidate(
            batch_id=bid, batch_no=f"B{bid}", source_year=year, origin_year=year,
            acquisition_method=method, remaining=remaining,
            valid_from=datetime(2020, 1, 1), valid_until=until, status="active",
        )

    def test_v1_carryover_before_issue_when_same_year_and_due(self):
        until = datetime(2026, 12, 31)
        c_issue = self._cand(1, 2025, "annual_issue", 100, until)
        c_carry = self._cand(2, 2025, "carryover", 100, until)
        r = select_batches([c_issue, c_carry], 50, rule_version="v1")
        assert r.is_satisfied
        assert r.lines[0].batch_id == 2  # v1：结转优先

    def test_v2_issue_before_carryover(self):
        until = datetime(2026, 12, 31)
        c_issue = self._cand(1, 2025, "annual_issue", 100, until)
        c_carry = self._cand(2, 2025, "carryover", 100, until)
        r = select_batches([c_issue, c_carry], 50, rule_version="v2")
        assert r.lines[0].batch_id == 1  # v2：核发优先

    def test_due_date_dominates_method(self):
        early = self._cand(1, 2025, "market_purchase", 100, datetime(2027, 6, 30))
        late = self._cand(2, 2025, "carryover", 100, datetime(2028, 12, 31))
        r = select_batches([early, late], 50, rule_version="v1")
        assert r.lines[0].batch_id == 1  # 更早到期优先，即使是购入批次

    def test_expired_candidate_excluded_and_shortfall(self):
        expired = self._cand(1, 2020, "annual_issue", 100, datetime(2021, 12, 31))
        active = self._cand(2, 2025, "annual_issue", 30, datetime(2027, 12, 31))
        r = select_batches([expired, active], 50, as_of=datetime(2026, 1, 1))
        assert not r.is_satisfied
        assert r.shortfall == 20
        assert any("过期" in e["reason"] for e in r.excluded)

    def test_lines_have_reasons(self):
        c = self._cand(1, 2025, "annual_issue", 100, datetime(2026, 12, 31))
        r = select_batches([c], 50, rule_version="v1")
        assert r.lines[0].reason
        assert "v1" in r.lines[0].reason


# ---------------------------------------------------------------------------
# 核发、余额、挂单预留
# ---------------------------------------------------------------------------

class TestBatchReservation:

    def test_confirm_issues_batch(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=1000, pc=8.0)
        batches = batch_service.get_batches(db, enterprise_id=ent.id)
        assert len(batches) == 1
        b = batches[0]
        assert b.acquisition_method == BatchAcquisitionMethod.ANNUAL_ISSUE
        assert b.original_amount > 0
        assert b.remaining_amount == b.original_amount
        assert batch_service.get_available_balance(db, ent.id) == round(b.original_amount, 2)

    def test_issue_is_idempotent_per_record(self, db):
        ent = _enterprise(db, "甲", "E1")
        rec = _positive_model(db, ent, 2025, "M1")
        # 重复补发不应产生第二个批次
        crud.ensure_all_batches_issued(db, ent.id)
        crud.ensure_all_batches_issued(db, ent.id)
        assert len(batch_service.get_batches(db, ent.id)) == 1

    def test_sell_order_freezes_batches(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=1000)
        total = batch_service.get_available_balance(db, ent.id)
        order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000, total_amount=total,
        ))
        assert batch_service.get_available_balance(db, ent.id) == 0.0
        summary = batch_service.get_balance_summary(db, ent.id)
        assert summary["frozen"] == total
        held = db.query(CreditBatchAllocation).filter_by(order_id=order.id).all()
        assert held and all(a.status == AllocationStatus.HELD for a in held)

    def test_cannot_oversell_beyond_available(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=100)
        total = batch_service.get_available_balance(db, ent.id)
        with pytest.raises(ValueError):
            crud.create_credit_order(db, schemas.CreditOrderCreate(
                enterprise_id=ent.id, year=2025, order_type=OrderType.SELL,
                unit_price=3000, total_amount=round(total + 100, 2),
            ))
        # 失败不留残单、余额不变
        assert batch_service.get_available_balance(db, ent.id) == total

    def test_frozen_amount_not_selectable(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=100)
        total = batch_service.get_available_balance(db, ent.id)
        crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000, total_amount=total,
        ))
        # 再挂一笔超过剩余可用（0）应失败
        with pytest.raises(ValueError):
            crud.create_credit_order(db, schemas.CreditOrderCreate(
                enterprise_id=ent.id, year=2025, order_type=OrderType.SELL,
                unit_price=3000, total_amount=10,
            ))


# ---------------------------------------------------------------------------
# 成交 / 撤单 / 部分成交
# ---------------------------------------------------------------------------

class TestMatchCancelReturn:

    def _setup_seller_buyer(self, db):
        seller = _enterprise(db, "卖方", "S1")
        buyer = _enterprise(db, "买方", "B1")
        _positive_model(db, seller, 2025, "SM", output=1000, pc=8.0)
        total = batch_service.get_available_balance(db, seller.id)
        sell = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000, total_amount=total,
        ))
        buy = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2025, order_type=OrderType.BUY,
            unit_price=3100, total_amount=round(total / 2, 2),
        ))
        return seller, buyer, sell, buy, total

    def test_partial_fill_consumes_seller_and_issues_buyer_batch(self, db):
        seller, buyer, sell, buy, total = self._setup_seller_buyer(db)
        half = round(total / 2, 2)
        txn, err = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell.id, buy_order_id=buy.id, credit_amount=half,
        ))
        assert err is None and txn is not None

        # 卖方：冻结减少、消耗增加
        sell_batches = batch_service.get_batches(db, enterprise_id=seller.id)
        frozen = round(sum(b.frozen_amount for b in sell_batches), 2)
        consumed = round(sum(b.consumed_amount for b in sell_batches), 2)
        assert frozen == round(total - half, 2)
        assert consumed == half

        # 买方：按成交行入库市场购入批次，总量守恒
        buyer_bal = batch_service.get_balance_summary(db, buyer.id)
        assert buyer_bal["available"] == half
        buy_batches = [b for b in batch_service.get_batches(db, buyer.id)
                       if b.acquisition_method == BatchAcquisitionMethod.MARKET_PURCHASE]
        assert buy_batches
        # 继承最初核发年度
        assert all(b.origin_year == 2025 for b in buy_batches)

        assert sell.status.value == "partial"
        assert buy.status.value == "filled"

    def test_cancel_releases_remaining_reservation(self, db):
        seller, buyer, sell, buy, total = self._setup_seller_buyer(db)
        half = round(total / 2, 2)
        crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell.id, buy_order_id=buy.id, credit_amount=half,
        ))
        crud.cancel_credit_order(db, sell.id)
        # 撤单后：剩余冻结全部回到可用，成交部分仍消耗
        bal = batch_service.get_balance_summary(db, seller.id)
        assert bal["available"] == round(total - half, 2)
        assert bal["frozen"] == 0.0
        assert bal["consumed"] == half

    def test_cancel_full_order_restores_all(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=100)
        total = batch_service.get_available_balance(db, ent.id)
        order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000, total_amount=total,
        ))
        crud.cancel_credit_order(db, order.id)
        assert batch_service.get_available_balance(db, ent.id) == total

    def test_return_transaction_restores_batches(self, db):
        seller, buyer, sell, buy, total = self._setup_seller_buyer(db)
        half = round(total / 2, 2)
        txn, _ = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell.id, buy_order_id=buy.id, credit_amount=half,
        ))
        result = crud.return_credit_transaction(db, txn.id)
        # 卖方恢复（原批次仍有效，恢复进原批次）
        assert result["seller_restored"]
        assert all(not x["new_batch"] for x in result["seller_restored"])
        bal = batch_service.get_balance_summary(db, seller.id)
        # 退回后卖方：可用=总-成交+退回 = total（买单只买了一半）
        assert bal["available"] == half
        assert bal["consumed"] == 0.0
        # 买方购入批次被冲回
        assert round(sum(x["amount"] for x in result["buyer_reversed"]), 2) == half
        assert batch_service.get_available_balance(db, buyer.id) == 0.0
        assert txn.status == "returned"

    def test_adjust_sell_order_up_then_down(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=1000)
        total = batch_service.get_available_balance(db, ent.id)
        quarter = round(total / 4, 2)
        order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000, total_amount=quarter,
        ))
        # 加挂
        crud.update_credit_order(db, order.id, schemas.CreditOrderUpdate(total_amount=quarter * 2))
        assert batch_service.get_balance_summary(db, ent.id)["frozen"] == round(quarter * 2, 2)
        # 减挂
        crud.update_credit_order(db, order.id, schemas.CreditOrderUpdate(total_amount=quarter))
        bal = batch_service.get_balance_summary(db, ent.id)
        assert bal["frozen"] == quarter
        assert bal["available"] == round(total - quarter, 2)


# ---------------------------------------------------------------------------
# 结转链
# ---------------------------------------------------------------------------

class TestCarryover:

    def test_carryover_creates_linked_child_with_ratio(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=1000, pc=8.0)
        total = batch_service.get_available_balance(db, ent.id)

        cos = crud.execute_yearly_carryover(db, 2025, 2026)
        assert len(cos) == 1
        co = cos[0]
        assert round(co.original_amount, 2) == total
        assert round(co.carryover_amount, 2) == round(total * 0.8, 2)

        children = [b for b in batch_service.get_batches(db, ent.id)
                    if b.acquisition_method == BatchAcquisitionMethod.CARRYOVER]
        assert len(children) == 1
        child = children[0]
        assert round(child.original_amount, 2) == round(total * 0.8, 2)
        assert child.source_year == 2026
        assert child.origin_year == 2025  # 最初核发年度保留

        lineage = batch_service.get_batch_lineage(db, child.id)
        assert lineage["ancestors"], "结转批次必须有来源链"
        assert round(sum(a["link_parent_amount"] for a in lineage["ancestors"]), 2) == total

    def test_chain_carryover_preserves_full_lineage(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=1000)
        crud.execute_yearly_carryover(db, 2025, 2026)
        crud.execute_yearly_carryover(db, 2026, 2027)

        # 2027 的结转批次应能沿链追溯到 2025 核发批次
        last = [b for b in batch_service.get_batches(db, ent.id)
                if b.acquisition_method == BatchAcquisitionMethod.CARRYOVER and b.source_year == 2027][0]
        lineage = batch_service.get_batch_lineage(db, last.id)
        methods = [a["acquisition_method"] for a in lineage["ancestors"]]
        # 链上应包含 2026 结转批次 与 2025 核发批次
        assert "carryover" in methods
        assert "annual_issue" in methods
        assert lineage["ancestors"][0]["origin_year"] == 2025


# ---------------------------------------------------------------------------
# 过期保护
# ---------------------------------------------------------------------------

class TestExpiry:

    def test_expire_skips_frozen(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2020, "OLD", output=100)
        batches = batch_service.get_batches(db, ent.id)
        b = batches[0]
        total = round(b.original_amount, 2)
        # 先把有效期延到未来以便挂单冻结，再模拟到期
        b.valid_until = datetime(2027, 12, 31)
        db.commit()
        order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=2020, order_type=OrderType.SELL,
            unit_price=3000, total_amount=total,
        ))
        b.valid_until = datetime(2021, 12, 31)
        db.commit()
        expired = crud.expire_due_batches(db, as_of=datetime(2026, 1, 1))
        assert expired == []  # 可用部分为0，无可过期
        bal = batch_service.get_balance_summary(db, ent.id, as_of=datetime(2026, 1, 1))
        assert bal["frozen"] == total  # 冻结量保留，未被误伤

        # 撤单释放后，数量回到可用（已过期），再跑作业应过期它
        crud.cancel_credit_order(db, order.id)
        expired = crud.expire_due_batches(db, as_of=datetime(2026, 1, 1))
        assert len(expired) == 1
        assert round(expired[0]["expired_amount"], 2) == total
        bal = batch_service.get_balance_summary(db, ent.id, as_of=datetime(2026, 1, 1))
        assert bal["available"] == 0.0
        assert bal["expired"] == total

    def test_expire_only_available_portion(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2020, "OLD", output=100)
        b = batch_service.get_batches(db, ent.id)[0]
        total = round(b.original_amount, 2)
        b.valid_until = datetime(2027, 12, 31)
        db.commit()
        # 冻结一半
        order = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=ent.id, year=2020, order_type=OrderType.SELL,
            unit_price=3000, total_amount=round(total / 2, 2),
        ))
        b.valid_until = datetime(2021, 12, 31)
        db.commit()
        expired = crud.expire_due_batches(db, as_of=datetime(2026, 1, 1))
        assert round(expired[0]["expired_amount"], 2) == round(total / 2, 2)
        assert round(expired[0]["frozen_preserved"], 2) == round(total / 2, 2)


# ---------------------------------------------------------------------------
# 履约
# ---------------------------------------------------------------------------

class TestFulfillment:

    def test_fulfillment_consumes_with_explanation(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=1000)
        total = batch_service.get_available_balance(db, ent.id)
        need = round(total / 3, 2)
        res = crud.fulfill_annual_obligation(db, ent.id, 2025, need)
        assert res["amount"] == need
        assert res["allocations"]
        assert res["allocations"][0]["reason"]
        bal = batch_service.get_balance_summary(db, ent.id)
        assert bal["consumed"] == need
        # 解释接口可读
        explanation = batch_service.explain_group(db, res["group_id"])
        assert explanation["lines"]
        assert explanation["candidates_snapshot"]["candidates"]


# ---------------------------------------------------------------------------
# 重放
# ---------------------------------------------------------------------------

class TestReplay:

    def test_replay_matches_historical(self, db):
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "M1", output=1000)
        total = batch_service.get_available_balance(db, ent.id)
        res = crud.fulfill_annual_obligation(db, ent.id, 2025, round(total / 2, 2))
        replay = batch_service.replay_group(db, res["group_id"])
        assert replay["matches_historical_result"] is True
        assert replay["historical_lines"] == replay["replayed_lines"]

    def test_replay_with_other_rule_version(self, db):
        # 同候选、同需求，v1 与 v2 在核发/结转优先上结果不同
        ent = _enterprise(db, "甲", "E1")
        _positive_model(db, ent, 2025, "ISS", output=1000)
        # 手工再造一个同年同到期的结转批次
        issue = batch_service.get_batches(db, ent.id)[0]
        carry = batch_service.issue_batch(
            db, ent.id, 2025, round(issue.original_amount, 2),
            BatchAcquisitionMethod.CARRYOVER,
            valid_from=datetime(2025, 1, 1), valid_until=issue.valid_until,
            origin_year=2025, batch_prefix="CAR",
        )
        # 用 v1 预留（结转优先）
        preview_v1 = crud.preview_batch_selection(db, ent.id, 10, "v1")
        preview_v2 = crud.preview_batch_selection(db, ent.id, 10, "v2")
        assert preview_v1["lines"][0]["acquisition_method"] == "carryover"
        assert preview_v2["lines"][0]["acquisition_method"] == "annual_issue"
        # 不实际消耗，仅验证对比重放接口存在且能产出差异
        group = batch_service.consume_direct(
            db, ent.id, AllocationPurpose.FULFILLMENT, 10, rule_version="v1")[0]
        alt = batch_service.replay_with_rule(db, group.id, "v2")
        assert alt["requested_rule_version"] == "v2"
        assert alt["lines"][0]["batch_no"] == issue.batch_no


# ---------------------------------------------------------------------------
# 守恒审计（端到端混合操作）
# ---------------------------------------------------------------------------

class TestConservation:

    def test_conservation_through_mixed_lifecycle(self, db):
        seller = _enterprise(db, "卖方", "S")
        buyer = _enterprise(db, "买方", "B")
        _positive_model(db, seller, 2025, "SM", output=1000, pc=8.0)
        total = batch_service.get_available_balance(db, seller.id)

        # 挂单卖 60%，成交一半（即30%），撤单释放剩余冻结
        sell = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000, total_amount=round(total * 0.6, 2),
        ))
        buy = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2025, order_type=OrderType.BUY,
            unit_price=3100, total_amount=round(total * 0.3, 2),
        ))
        txn, _ = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell.id, buy_order_id=buy.id,
            credit_amount=round(total * 0.3, 2),
        ))
        crud.cancel_credit_order(db, sell.id)

        audit = batch_service.audit_conservation(db)
        assert audit["conservation_ok"], audit["batch_violations"]
        assert audit["freeze_ok"], audit["freeze_violations"]
        assert audit["transaction_balance_ok"], audit["transaction_violations"]

        # 退回后仍守恒
        crud.return_credit_transaction(db, txn.id)
        audit = batch_service.audit_conservation(db)
        assert audit["conservation_ok"], audit["batch_violations"]
        assert audit["freeze_ok"], audit["freeze_violations"]
        assert audit["transaction_balance_ok"], audit["transaction_violations"]

        # 每个批次：原始量 = 剩余+冻结+消耗+过期
        for b in batch_service.get_batches(db):
            lhs = round(b.original_amount, 2)
            rhs = round(b.remaining_amount + b.frozen_amount
                        + b.consumed_amount + b.expired_amount, 2)
            assert abs(lhs - rhs) <= 0.02, (b.batch_no, lhs, rhs)

    def test_explain_transaction_shows_why(self, db):
        seller = _enterprise(db, "卖方", "S")
        buyer = _enterprise(db, "买方", "B")
        _positive_model(db, seller, 2025, "SM", output=1000)
        total = batch_service.get_available_balance(db, seller.id)
        sell = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000, total_amount=total,
        ))
        buy = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2025, order_type=OrderType.BUY,
            unit_price=3100, total_amount=round(total / 2, 2),
        ))
        txn, _ = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell.id, buy_order_id=buy.id,
            credit_amount=round(total / 2, 2),
        ))
        explanation = batch_service.explain_transaction(db, txn.id)
        assert explanation["selected_lines"]
        assert all(line["reason"] for line in explanation["selected_lines"])
        assert explanation["buyer_received_batches"]


class TestReturnToExpiredParent:

    def test_return_creates_new_batch_when_parent_expired(self, db):
        seller = _enterprise(db, "卖方", "S2")
        buyer = _enterprise(db, "买方", "B2")
        _positive_model(db, seller, 2020, "SM", output=100)
        b = batch_service.get_batches(db, seller.id)[0]
        # 先把有效期延到未来以便挂单，成交后再令父批次过期
        b.valid_until = datetime(2027, 12, 31)
        db.commit()
        total = batch_service.get_available_balance(db, seller.id)
        sell = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2020, order_type=OrderType.SELL,
            unit_price=3000, total_amount=total,
        ))
        buy = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2020, order_type=OrderType.BUY,
            unit_price=3100, total_amount=total,
        ))
        txn, _ = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell.id, buy_order_id=buy.id, credit_amount=total,
        ))
        parent = batch_service.get_batches(db, seller.id)[0]
        assert parent.status == BatchStatus.EXHAUSTED
        # 令父批次在退回时已过有效期
        parent.valid_until = datetime(2021, 12, 31)
        db.commit()
        result = crud.return_credit_transaction(db, txn.id)
        assert result["seller_restored"][0]["new_batch"] is True
        ret_batches = [x for x in batch_service.get_batches(db, seller.id)
                       if x.acquisition_method == BatchAcquisitionMethod.RETURN]
        assert len(ret_batches) == 1
        assert round(ret_batches[0].original_amount, 2) == total
        assert ret_batches[0].parent_batch_id == parent.id
        assert ret_batches[0].origin_year == 2020
        audit = batch_service.audit_conservation(db)
        assert audit["conservation_ok"] and audit["freeze_ok"]

    def test_return_revives_exhausted_but_valid_parent(self, db):
        seller = _enterprise(db, "卖方", "S3")
        buyer = _enterprise(db, "买方", "B3")
        _positive_model(db, seller, 2025, "SM", output=100)
        total = batch_service.get_available_balance(db, seller.id)
        sell = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=seller.id, year=2025, order_type=OrderType.SELL,
            unit_price=3000, total_amount=total,
        ))
        buy = crud.create_credit_order(db, schemas.CreditOrderCreate(
            enterprise_id=buyer.id, year=2025, order_type=OrderType.BUY,
            unit_price=3100, total_amount=total,
        ))
        txn, _ = crud.match_orders_by_id(db, schemas.CreditOrderMatchRequest(
            sell_order_id=sell.id, buy_order_id=buy.id, credit_amount=total,
        ))
        parent = batch_service.get_batches(db, seller.id)[0]
        assert parent.status == BatchStatus.EXHAUSTED
        # 父批次耗尽但仍在有效期：退回应直接复活原批次，不新建
        result = crud.return_credit_transaction(db, txn.id)
        assert result["seller_restored"][0]["new_batch"] is False
        assert not [x for x in batch_service.get_batches(db, seller.id)
                    if x.acquisition_method == BatchAcquisitionMethod.RETURN]
        db.refresh(parent)
        assert round(parent.remaining_amount, 2) == total
        assert parent.status == BatchStatus.ACTIVE
        audit = batch_service.audit_conservation(db)
        assert audit["conservation_ok"]
