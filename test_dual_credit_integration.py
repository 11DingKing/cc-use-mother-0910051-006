import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import crud, schemas, rules
from app.models import CreditRecordStatus
from app.rules import (
    calculate_power_consumption_limit,
    calculate_unit_credit,
    calculate_total_credit,
    POWER_CONSUMPTION_LIMIT_TIERS,
    CREDIT_MULTIPLIER,
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


class TestDualCreditFullChain:
    """双积分核算全链路集成测试"""

    TEST_YEAR = 2025

    def test_full_credit_calculation_chain(self, db):
        """完整核算链：录入车型→定限值→算单车积分→汇总→撮合"""
        ent_a = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="测试车企A",
                short_name="车企A",
                credit_code="TEST001",
            ),
        )
        ent_b = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="测试车企B",
                short_name="车企B",
                credit_code="TEST002",
            ),
        )
        ent_c = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="测试车企C",
                short_name="车企C",
                credit_code="TEST003",
            ),
        )
        ent_d = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="测试车企D",
                short_name="车企D",
                credit_code="TEST004",
            ),
        )

        model_a1 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_a.id,
                model_name="车型A1-普通正积分",
                model_code="A001",
                curb_weight=800.0,
                power_consumption=8.0,
                range=400.0,
                annual_output=1000,
                production_year=self.TEST_YEAR,
            ),
        )
        expected_limit_a1 = 10.5
        expected_unit_credit_a1 = round(
            (expected_limit_a1 - 8.0) / expected_limit_a1 * CREDIT_MULTIPLIER, 4
        )
        expected_total_a1 = round(expected_unit_credit_a1 * 1000, 2)

        assert calculate_power_consumption_limit(800.0) == expected_limit_a1
        assert calculate_unit_credit(8.0, expected_limit_a1) == expected_unit_credit_a1
        assert calculate_total_credit(expected_unit_credit_a1, 1000) == expected_total_a1

        model_a2 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_a.id,
                model_name="车型A2-临界重量1000.1kg",
                model_code="A002",
                curb_weight=1000.1,
                power_consumption=10.0,
                range=500.0,
                annual_output=500,
                production_year=self.TEST_YEAR,
            ),
        )
        expected_limit_a2 = 11.5
        expected_unit_credit_a2 = round(
            (expected_limit_a2 - 10.0) / expected_limit_a2 * CREDIT_MULTIPLIER, 4
        )
        expected_total_a2 = round(expected_unit_credit_a2 * 500, 2)

        assert calculate_power_consumption_limit(1000.1) == expected_limit_a2
        assert calculate_unit_credit(10.0, expected_limit_a2) == expected_unit_credit_a2
        assert calculate_total_credit(expected_unit_credit_a2, 500) == expected_total_a2

        model_a3 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_a.id,
                model_name="车型A3-零产量",
                model_code="A003",
                curb_weight=900.0,
                power_consumption=9.0,
                range=350.0,
                annual_output=0,
                production_year=self.TEST_YEAR,
            ),
        )
        expected_limit_a3 = 10.5
        expected_unit_credit_a3 = round(
            (expected_limit_a3 - 9.0) / expected_limit_a3 * CREDIT_MULTIPLIER, 4
        )
        expected_total_a3 = 0.0

        assert calculate_total_credit(expected_unit_credit_a3, 0) == expected_total_a3

        model_b1 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_b.id,
                model_name="车型B1-普通负积分",
                model_code="B001",
                curb_weight=1500.0,
                power_consumption=15.0,
                range=300.0,
                annual_output=800,
                production_year=self.TEST_YEAR,
            ),
        )
        expected_limit_b1 = 13.5
        expected_unit_credit_b1 = round(
            (expected_limit_b1 - 15.0) / expected_limit_b1 * CREDIT_MULTIPLIER, 4
        )
        expected_total_b1 = round(expected_unit_credit_b1 * 800, 2)

        assert calculate_power_consumption_limit(1500.0) == expected_limit_b1
        assert calculate_unit_credit(15.0, expected_limit_b1) == expected_unit_credit_b1
        assert calculate_total_credit(expected_unit_credit_b1, 800) == expected_total_b1

        model_b2 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_b.id,
                model_name="车型B2-临界重量2000.1kg",
                model_code="B002",
                curb_weight=2000.1,
                power_consumption=18.0,
                range=280.0,
                annual_output=600,
                production_year=self.TEST_YEAR,
            ),
        )
        expected_limit_b2 = 17.0
        expected_unit_credit_b2 = round(
            (expected_limit_b2 - 18.0) / expected_limit_b2 * CREDIT_MULTIPLIER, 4
        )
        expected_total_b2 = round(expected_unit_credit_b2 * 600, 2)

        assert calculate_power_consumption_limit(2000.1) == expected_limit_b2
        assert calculate_unit_credit(18.0, expected_limit_b2) == expected_unit_credit_b2
        assert calculate_total_credit(expected_unit_credit_b2, 600) == expected_total_b2

        model_c1 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_c.id,
                model_name="车型C1-非确认状态",
                model_code="C001",
                curb_weight=1200.1,
                power_consumption=10.0,
                range=450.0,
                annual_output=1000,
                production_year=self.TEST_YEAR,
            ),
        )
        expected_limit_c1 = 12.5
        expected_unit_credit_c1 = round(
            (expected_limit_c1 - 10.0) / expected_limit_c1 * CREDIT_MULTIPLIER, 4
        )
        expected_total_c1 = round(expected_unit_credit_c1 * 1000, 2)

        assert calculate_power_consumption_limit(1200.1) == expected_limit_c1
        assert calculate_unit_credit(10.0, expected_limit_c1) == expected_unit_credit_c1

        model_d1 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_d.id,
                model_name="车型D1-临界重量3000.1kg",
                model_code="D001",
                curb_weight=3000.1,
                power_consumption=23.0,
                range=250.0,
                annual_output=200,
                production_year=self.TEST_YEAR,
            ),
        )
        expected_limit_d1 = 22.0
        expected_unit_credit_d1 = round(
            (expected_limit_d1 - 23.0) / expected_limit_d1 * CREDIT_MULTIPLIER, 4
        )
        expected_total_d1 = round(expected_unit_credit_d1 * 200, 2)

        assert calculate_power_consumption_limit(3000.1) == expected_limit_d1

        for model in [model_a1, model_a2, model_a3, model_b1, model_b2, model_c1, model_d1]:
            crud.create_credit_record(db, model.id, self.TEST_YEAR)

        all_records = crud.get_credit_records(db, year=self.TEST_YEAR)
        assert len(all_records) == 7

        record_a1 = crud.get_credit_records(db, enterprise_id=ent_a.id, year=self.TEST_YEAR)[0]
        record_a2 = crud.get_credit_records(db, enterprise_id=ent_a.id, year=self.TEST_YEAR)[1]
        record_a3 = crud.get_credit_records(db, enterprise_id=ent_a.id, year=self.TEST_YEAR)[2]
        record_b1 = crud.get_credit_records(db, enterprise_id=ent_b.id, year=self.TEST_YEAR)[0]
        record_b2 = crud.get_credit_records(db, enterprise_id=ent_b.id, year=self.TEST_YEAR)[1]
        record_c1 = crud.get_credit_records(db, enterprise_id=ent_c.id, year=self.TEST_YEAR)[0]
        record_d1 = crud.get_credit_records(db, enterprise_id=ent_d.id, year=self.TEST_YEAR)[0]

        assert record_a1.power_consumption_limit == expected_limit_a1
        assert record_a1.unit_credit == expected_unit_credit_a1
        assert record_a1.total_credit == expected_total_a1

        assert record_a2.power_consumption_limit == expected_limit_a2
        assert record_a2.unit_credit == expected_unit_credit_a2
        assert record_a2.total_credit == expected_total_a2

        assert record_a3.total_credit == expected_total_a3

        assert record_b1.power_consumption_limit == expected_limit_b1
        assert record_b1.unit_credit == expected_unit_credit_b1
        assert record_b1.total_credit == expected_total_b1

        assert record_b2.power_consumption_limit == expected_limit_b2
        assert record_b2.unit_credit == expected_unit_credit_b2
        assert record_b2.total_credit == expected_total_b2

        assert record_c1.power_consumption_limit == expected_limit_c1
        assert record_c1.total_credit == expected_total_c1

        summary_a_calc = crud.calculate_enterprise_credit_summary(db, ent_a.id, self.TEST_YEAR)
        summary_b_calc = crud.calculate_enterprise_credit_summary(db, ent_b.id, self.TEST_YEAR)
        summary_c_calc = crud.calculate_enterprise_credit_summary(db, ent_c.id, self.TEST_YEAR)
        summary_d_calc = crud.calculate_enterprise_credit_summary(db, ent_d.id, self.TEST_YEAR)

        assert summary_a_calc.total_positive_credit == 0.0
        assert summary_a_calc.total_negative_credit == 0.0
        assert summary_b_calc.total_positive_credit == 0.0
        assert summary_b_calc.total_negative_credit == 0.0
        assert summary_c_calc.total_positive_credit == 0.0
        assert summary_d_calc.total_positive_credit == 0.0

        for record in [record_a1, record_a2, record_a3, record_b1, record_b2, record_d1]:
            crud.update_credit_record_status(db, record.id, CreditRecordStatus.PUBLICIZED)
            crud.update_credit_record_status(db, record.id, CreditRecordStatus.CONFIRMED)

        summary_a = crud.calculate_enterprise_credit_summary(db, ent_a.id, self.TEST_YEAR)
        summary_b = crud.calculate_enterprise_credit_summary(db, ent_b.id, self.TEST_YEAR)
        summary_c = crud.calculate_enterprise_credit_summary(db, ent_c.id, self.TEST_YEAR)
        summary_d = crud.calculate_enterprise_credit_summary(db, ent_d.id, self.TEST_YEAR)

        expected_a_positive = round(expected_total_a1 + expected_total_a2 + expected_total_a3, 2)
        expected_a_negative = 0.0
        expected_a_net = round(expected_a_positive + expected_a_negative, 2)
        expected_a_required = 0.0
        expected_a_gap = 0.0
        expected_a_surplus = expected_a_net

        assert summary_a.total_positive_credit == expected_a_positive
        assert summary_a.total_negative_credit == expected_a_negative
        assert summary_a.net_credit == expected_a_net
        assert summary_a.required_credit == expected_a_required
        assert summary_a.credit_gap == expected_a_gap
        assert summary_a.credit_surplus == expected_a_surplus
        assert summary_a.model_count == 3

        expected_b_positive = 0.0
        expected_b_negative = round(expected_total_b1 + expected_total_b2, 2)
        expected_b_net = round(expected_b_positive + expected_b_negative, 2)
        expected_b_required = round(abs(expected_b_negative), 2)
        expected_b_gap = round(expected_b_required - expected_b_positive, 2)
        expected_b_surplus = 0.0

        assert summary_b.total_positive_credit == expected_b_positive
        assert summary_b.total_negative_credit == expected_b_negative
        assert summary_b.net_credit == expected_b_net
        assert summary_b.required_credit == expected_b_required
        assert summary_b.credit_gap == expected_b_gap
        assert summary_b.credit_surplus == expected_b_surplus
        assert summary_b.model_count == 2

        assert summary_c.total_positive_credit == 0.0
        assert summary_c.total_negative_credit == 0.0
        assert summary_c.model_count == 0

        expected_d_positive = 0.0
        expected_d_negative = round(expected_total_d1, 2)
        expected_d_net = expected_d_negative
        expected_d_required = round(abs(expected_d_negative), 2)
        expected_d_gap = expected_d_required
        expected_d_surplus = 0.0

        assert summary_d.total_positive_credit == expected_d_positive
        assert summary_d.total_negative_credit == expected_d_negative
        assert summary_d.net_credit == expected_d_net
        assert summary_d.credit_gap == expected_d_gap
        assert summary_d.credit_surplus == expected_d_surplus

        summaries_before = [summary_a, summary_b, summary_c, summary_d]
        total_positive_before = sum(s.total_positive_credit for s in summaries_before)
        total_negative_before = sum(s.total_negative_credit for s in summaries_before)
        total_surplus_before = sum(s.credit_surplus for s in summaries_before)
        total_gap_before = sum(s.credit_gap for s in summaries_before)

        transactions, remaining_gap, remaining_surplus = crud.match_and_execute_transactions(
            db, year=self.TEST_YEAR, unit_price=3000.0
        )

        total_transferred = sum(t.credit_amount for t in transactions)
        total_transferred_out = sum(
            t.credit_amount for t in transactions if t.from_enterprise_id == ent_a.id
        )
        total_transferred_in_b = sum(
            t.credit_amount for t in transactions if t.to_enterprise_id == ent_b.id
        )
        total_transferred_in_d = sum(
            t.credit_amount for t in transactions if t.to_enterprise_id == ent_d.id
        )

        assert total_transferred_out == total_transferred_in_b + total_transferred_in_d
        assert round(total_transferred, 2) == round(total_transferred_out, 2)

        expected_transfer_to_b = min(expected_a_surplus, expected_b_gap)
        remaining_after_b = round(expected_a_surplus - expected_transfer_to_b, 2)
        expected_transfer_to_d = min(remaining_after_b, expected_d_gap)
        expected_total_transfer = round(expected_transfer_to_b + expected_transfer_to_d, 2)

        assert round(total_transferred, 2) == expected_total_transfer

        summary_a_after = crud.calculate_enterprise_credit_summary(db, ent_a.id, self.TEST_YEAR)
        summary_b_after = crud.calculate_enterprise_credit_summary(db, ent_b.id, self.TEST_YEAR)
        summary_d_after = crud.calculate_enterprise_credit_summary(db, ent_d.id, self.TEST_YEAR)

        assert round(summary_a_after.credit_surplus, 2) == round(expected_a_surplus, 2)
        assert round(summary_b_after.credit_gap, 2) == round(expected_b_gap, 2)
        assert round(summary_d_after.credit_gap, 2) == round(expected_d_gap, 2)

        total_net_after = (
            summary_a_after.net_credit
            + summary_b_after.net_credit
            + summary_d_after.net_credit
        )
        total_net_before = expected_a_net + expected_b_net + expected_d_net
        assert round(total_net_after, 2) == round(total_net_before, 2)

        total_positive_after = (
            summary_a_after.total_positive_credit
            + summary_b_after.total_positive_credit
            + summary_d_after.total_positive_credit
        )
        total_negative_after = (
            summary_a_after.total_negative_credit
            + summary_b_after.total_negative_credit
            + summary_d_after.total_negative_credit
        )
        assert round(total_positive_after, 2) == round(total_positive_before, 2)
        assert round(total_negative_after, 2) == round(total_negative_before, 2)

        total_surplus_transferred = sum(
            t.credit_amount for t in transactions if t.from_enterprise_id == ent_a.id
        )
        total_deficit_filled_b = sum(
            t.credit_amount for t in transactions if t.to_enterprise_id == ent_b.id
        )
        total_deficit_filled_d = sum(
            t.credit_amount for t in transactions if t.to_enterprise_id == ent_d.id
        )

        assert round(total_surplus_transferred, 2) == round(
            total_deficit_filled_b + total_deficit_filled_d, 2)

        expected_remaining_surplus = round(
            expected_a_surplus - total_surplus_transferred, 2)
        expected_remaining_gap_b = round(expected_b_gap - total_deficit_filled_b, 2)
        expected_remaining_gap_d = round(expected_d_gap - total_deficit_filled_d, 2)

        assert round(remaining_surplus, 2) == round(
            max(0, expected_remaining_surplus), 2)
        assert round(remaining_gap, 2) == round(
            max(0, expected_remaining_gap_b) + max(0, expected_remaining_gap_d), 2)

    def test_weight_boundary_tiers(self, db):
        """测试所有重量档位边界值"""
        boundary_cases = [
            (0.0, 10.5),
            (999.9, 10.5),
            (1000.0, 10.5),
            (1000.1, 11.5),
            (1199.9, 11.5),
            (1200.0, 11.5),
            (1200.1, 12.5),
            (1399.9, 12.5),
            (1400.0, 12.5),
            (1400.1, 13.5),
            (1599.9, 13.5),
            (1600.0, 13.5),
            (1600.1, 14.5),
            (1799.9, 14.5),
            (1800.0, 14.5),
            (1800.1, 15.5),
            (1999.9, 15.5),
            (2000.0, 15.5),
            (2000.1, 17.0),
            (2299.9, 17.0),
            (2300.0, 17.0),
            (2300.1, 18.5),
            (2599.9, 18.5),
            (2600.0, 18.5),
            (2600.1, 20.0),
            (2999.9, 20.0),
            (3000.0, 20.0),
            (3000.1, 22.0),
            (5000.0, 22.0),
        ]

        for weight, expected_limit in boundary_cases:
            actual_limit = calculate_power_consumption_limit(weight)
            assert actual_limit == expected_limit, (
                f"重量 {weight}kg 应匹配限值 {expected_limit}，实际为 {actual_limit}"
            )

    def test_zero_output_models(self, db):
        """测试产量为零的车型不产生积分"""
        ent = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="零产量测试车企",
                short_name="零产量",
                credit_code="ZERO001",
            ),
        )

        model_zero = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent.id,
                model_name="零产量车型",
                model_code="ZERO-M01",
                curb_weight=1500.0,
                power_consumption=10.0,
                range=500.0,
                annual_output=0,
                production_year=self.TEST_YEAR,
            ),
        )

        limit = calculate_power_consumption_limit(1500.0)
        unit_credit = calculate_unit_credit(10.0, limit)
        total_credit = calculate_total_credit(unit_credit, 0)

        assert total_credit == 0.0

        record = crud.create_credit_record(db, model_zero.id, self.TEST_YEAR)
        assert record.total_credit == 0.0

        crud.update_credit_record_status(db, record.id, CreditRecordStatus.PUBLICIZED)
        crud.update_credit_record_status(db, record.id, CreditRecordStatus.CONFIRMED)

        summary = crud.calculate_enterprise_credit_summary(db, ent.id, self.TEST_YEAR)
        assert summary.total_positive_credit == 0.0
        assert summary.total_negative_credit == 0.0
        assert summary.net_credit == 0.0

    def test_only_confirmed_records_counted(self, db):
        """测试只有确认状态的记录才计入汇总"""
        ent = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="状态测试车企",
                short_name="状态测试",
                credit_code="STATUS001",
            ),
        )

        model_calculated = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent.id,
                model_name="仅核算状态",
                model_code="ST-CALC",
                curb_weight=1000.0,
                power_consumption=8.0,
                range=400.0,
                annual_output=100,
                production_year=self.TEST_YEAR,
            ),
        )

        model_publicized = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent.id,
                model_name="仅公示状态",
                model_code="ST-PUB",
                curb_weight=1200.0,
                power_consumption=9.0,
                range=450.0,
                annual_output=200,
                production_year=self.TEST_YEAR,
            ),
        )

        model_confirmed = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent.id,
                model_name="已确认状态",
                model_code="ST-CONF",
                curb_weight=1400.1,
                power_consumption=10.0,
                range=500.0,
                annual_output=300,
                production_year=self.TEST_YEAR,
            ),
        )

        for model in [model_calculated, model_publicized, model_confirmed]:
            crud.create_credit_record(db, model.id, self.TEST_YEAR)

        records = crud.get_credit_records(db, enterprise_id=ent.id, year=self.TEST_YEAR)
        assert len(records) == 3

        record_calc = [r for r in records if r.vehicle_model_id == model_calculated.id][0]
        record_pub = [r for r in records if r.vehicle_model_id == model_publicized.id][0]
        record_conf = [r for r in records if r.vehicle_model_id == model_confirmed.id][0]

        crud.update_credit_record_status(db, record_pub.id, CreditRecordStatus.PUBLICIZED)
        crud.update_credit_record_status(db, record_conf.id, CreditRecordStatus.PUBLICIZED)
        crud.update_credit_record_status(db, record_conf.id, CreditRecordStatus.CONFIRMED)

        summary = crud.calculate_enterprise_credit_summary(db, ent.id, self.TEST_YEAR)

        expected_conf_limit = calculate_power_consumption_limit(1400.1)
        expected_conf_unit = calculate_unit_credit(10.0, expected_conf_limit)
        expected_conf_total = calculate_total_credit(expected_conf_unit, 300)

        assert summary.model_count == 1
        assert summary.total_positive_credit == expected_conf_total
        assert summary.total_negative_credit == 0.0

    def test_no_matchable_partners(self, db):
        """测试企业间没有可撮合对象时的处理"""
        ent_all_positive1 = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="全正积分企业1",
                short_name="全正1",
                credit_code="POS001",
            ),
        )
        ent_all_positive2 = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="全正积分企业2",
                short_name="全正2",
                credit_code="POS002",
            ),
        )

        model1 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_all_positive1.id,
                model_name="正积分车型1",
                model_code="POS-M01",
                curb_weight=1000.0,
                power_consumption=8.0,
                range=400.0,
                annual_output=1000,
                production_year=self.TEST_YEAR,
            ),
        )

        model2 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_all_positive2.id,
                model_name="正积分车型2",
                model_code="POS-M02",
                curb_weight=1200.0,
                power_consumption=9.0,
                range=450.0,
                annual_output=500,
                production_year=self.TEST_YEAR,
            ),
        )

        for model in [model1, model2]:
            record = crud.create_credit_record(db, model.id, self.TEST_YEAR)
            crud.update_credit_record_status(db, record.id, CreditRecordStatus.PUBLICIZED)
            crud.update_credit_record_status(db, record.id, CreditRecordStatus.CONFIRMED)

        transactions, remaining_gap, remaining_surplus = crud.match_and_execute_transactions(
            db, year=self.TEST_YEAR, unit_price=3000.0
        )

        assert len(transactions) == 0
        assert remaining_gap == 0.0

        summaries = crud.calculate_all_enterprise_summaries(db, year=self.TEST_YEAR)
        total_surplus = sum(s.credit_surplus for s in summaries)
        assert round(remaining_surplus, 2) == round(total_surplus, 2)

        ent_all_negative = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="全负积分企业",
                short_name="全负",
                credit_code="NEG001",
            ),
        )

        model_neg = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_all_negative.id,
                model_name="负积分车型",
                model_code="NEG-M01",
                curb_weight=1500.0,
                power_consumption=20.0,
                range=200.0,
                annual_output=1000,
                production_year=self.TEST_YEAR,
            ),
        )

        record_neg = crud.create_credit_record(db, model_neg.id, self.TEST_YEAR)

        summary_before = crud.calculate_enterprise_credit_summary(
            db, ent_all_negative.id, self.TEST_YEAR
        )
        assert summary_before.credit_gap == 0.0

        crud.update_credit_record_status(db, record_neg.id, CreditRecordStatus.PUBLICIZED)
        crud.update_credit_record_status(db, record_neg.id, CreditRecordStatus.CONFIRMED)

        db.expire_all()

        transactions2, remaining_gap2, remaining_surplus2 = crud.match_and_execute_transactions(
            db, year=self.TEST_YEAR, unit_price=3000.0
        )

        assert len(transactions2) > 0

        total_transferred = sum(t.credit_amount for t in transactions2)
        total_surplus_before = sum(
            s.credit_surplus
            for s in crud.calculate_all_enterprise_summaries(db, year=self.TEST_YEAR)
        )
        total_gap_before = sum(
            s.credit_gap
            for s in crud.calculate_all_enterprise_summaries(db, year=self.TEST_YEAR)
        )

        assert round(total_transferred, 2) <= round(total_surplus_before + remaining_surplus2, 2)
        assert round(total_transferred, 2) <= round(total_gap_before + remaining_gap2, 2)

    def test_credit_conservation_after_matching(self, db):
        """撮合后积分守恒验证：转出=转入，总量不变"""
        ent_surplus = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="结余企业",
                short_name="结余",
                credit_code="CONS001",
            ),
        )
        ent_deficit1 = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="缺口企业1",
                short_name="缺口1",
                credit_code="CONS002",
            ),
        )
        ent_deficit2 = crud.create_enterprise(
            db,
            schemas.EnterpriseCreate(
                name="缺口企业2",
                short_name="缺口2",
                credit_code="CONS003",
            ),
        )

        model_s = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_surplus.id,
                model_name="结余车型",
                model_code="CONS-S",
                curb_weight=1000.0,
                power_consumption=8.0,
                range=500.0,
                annual_output=1000,
                production_year=self.TEST_YEAR,
            ),
        )

        model_d1 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_deficit1.id,
                model_name="缺口车型1",
                model_code="CONS-D1",
                curb_weight=1500.0,
                power_consumption=18.0,
                range=250.0,
                annual_output=300,
                production_year=self.TEST_YEAR,
            ),
        )

        model_d2 = crud.create_vehicle_model(
            db,
            schemas.VehicleModelCreate(
                enterprise_id=ent_deficit2.id,
                model_name="缺口车型2",
                model_code="CONS-D2",
                curb_weight=2000.1,
                power_consumption=22.0,
                range=220.0,
                annual_output=200,
                production_year=self.TEST_YEAR,
            ),
        )

        records = []
        for model in [model_s, model_d1, model_d2]:
            record = crud.create_credit_record(db, model.id, self.TEST_YEAR)
            records.append(record)
            crud.update_credit_record_status(db, record.id, CreditRecordStatus.PUBLICIZED)
            crud.update_credit_record_status(db, record.id, CreditRecordStatus.CONFIRMED)

        summaries_before = crud.calculate_all_enterprise_summaries(db, year=self.TEST_YEAR)
        total_positive_before = sum(s.total_positive_credit for s in summaries_before)
        total_negative_before = sum(s.total_negative_credit for s in summaries_before)
        total_net_before = sum(s.net_credit for s in summaries_before)
        total_surplus_before = sum(s.credit_surplus for s in summaries_before)
        total_gap_before = sum(s.credit_gap for s in summaries_before)

        transactions, remaining_gap, remaining_surplus = crud.match_and_execute_transactions(
            db, year=self.TEST_YEAR, unit_price=3000.0
        )

        total_transferred_out = sum(t.credit_amount for t in transactions)
        total_transferred_in = sum(t.credit_amount for t in transactions)

        assert round(total_transferred_out, 2) == round(total_transferred_in, 2)

        by_enterprise_out = {}
        by_enterprise_in = {}
        for t in transactions:
            by_enterprise_out[t.from_enterprise_id] = (
                by_enterprise_out.get(t.from_enterprise_id, 0.0) + t.credit_amount
            )
            by_enterprise_in[t.to_enterprise_id] = (
                by_enterprise_in.get(t.to_enterprise_id, 0.0) + t.credit_amount
            )

        assert round(by_enterprise_out.get(ent_surplus.id, 0.0), 2) == round(
            total_transferred_out, 2
        )
        assert round(
            by_enterprise_in.get(ent_deficit1.id, 0.0)
            + by_enterprise_in.get(ent_deficit2.id, 0.0),
            2,
        ) == round(total_transferred_in, 2)

        db.expire_all()

        summaries_after = crud.calculate_all_enterprise_summaries(db, year=self.TEST_YEAR)
        total_positive_after = sum(s.total_positive_credit for s in summaries_after)
        total_negative_after = sum(s.total_negative_credit for s in summaries_after)
        total_net_after = sum(s.net_credit for s in summaries_after)

        assert round(total_positive_after, 2) == round(total_positive_before, 2)
        assert round(total_negative_after, 2) == round(total_negative_before, 2)
        assert round(total_net_after, 2) == round(total_net_before, 2)

        assert round(remaining_surplus, 2) == round(
            total_surplus_before - total_transferred_out, 2
        )
        assert round(remaining_gap, 2) == round(total_gap_before - total_transferred_in, 2)
