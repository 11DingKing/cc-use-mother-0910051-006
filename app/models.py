import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, Boolean, Text
from sqlalchemy.orm import relationship

from .database import Base


class CreditRecordStatus(str, enum.Enum):
    CALCULATED = "calculated"
    PUBLICIZED = "publicized"
    CONFIRMED = "confirmed"


class OrderType(str, enum.Enum):
    SELL = "sell"
    BUY = "buy"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"


class CarryoverStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class BatchAcquisitionMethod(str, enum.Enum):
    ANNUAL_ISSUE = "annual_issue"       # 当年核发
    CARRYOVER = "carryover"             # 历年结转
    MARKET_PURCHASE = "market_purchase"  # 市场购入
    RETURN = "return"                    # 退回到已过期父批次后形成的恢复批次


class BatchStatus(str, enum.Enum):
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    EXPIRED = "expired"


class AllocationPurpose(str, enum.Enum):
    RESERVE = "reserve"        # 挂单预留（冻结）
    FULFILLMENT = "fulfillment"  # 年度履约
    SELL = "sell"              # 出售成交
    CARRYOVER = "carryover"    # 结转出库
    RETURN = "return"          # 退回恢复


class AllocationStatus(str, enum.Enum):
    HELD = "held"
    CONSUMED = "consumed"
    RELEASED = "released"
    RESTORED = "restored"


class Enterprise(Base):
    __tablename__ = "enterprises"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    short_name = Column(String(50), unique=True, index=True)
    credit_code = Column(String(50), unique=True, index=True)
    address = Column(String(200))
    contact_person = Column(String(50))
    contact_phone = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vehicle_models = relationship("VehicleModel", back_populates="enterprise")
    credit_transactions_from = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.from_enterprise_id",
        back_populates="from_enterprise"
    )
    credit_transactions_to = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.to_enterprise_id",
        back_populates="to_enterprise"
    )
    credit_orders = relationship(
        "CreditOrder",
        foreign_keys="CreditOrder.enterprise_id",
        back_populates="enterprise"
    )
    credit_carryovers_from = relationship(
        "CreditCarryover",
        foreign_keys="CreditCarryover.enterprise_id",
        back_populates="enterprise"
    )


class VehicleModel(Base):
    __tablename__ = "vehicle_models"

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    model_name = Column(String(100), nullable=False, index=True)
    model_code = Column(String(50), unique=True, nullable=False, index=True)
    curb_weight = Column(Float, nullable=False, comment="整备质量(kg)")
    power_consumption = Column(Float, nullable=False, comment="百公里电耗(kWh/100km)")
    range = Column(Float, nullable=False, comment="续航里程(km)")
    annual_output = Column(Integer, nullable=False, comment="年产量(辆)")
    production_year = Column(Integer, nullable=False, comment="生产年份")
    is_suspected_weight_manipulation = Column(Boolean, default=False, comment="是否疑似堆重量放宽限值")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    enterprise = relationship("Enterprise", back_populates="vehicle_models")
    credit_records = relationship("CreditRecord", back_populates="vehicle_model")


class CreditRecord(Base):
    __tablename__ = "credit_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="核算年份")
    power_consumption_limit = Column(Float, nullable=False, comment="电耗限值(kWh/100km)")
    actual_power_consumption = Column(Float, nullable=False, comment="实际电耗(kWh/100km)")
    unit_credit = Column(Float, nullable=False, comment="单车积分(分/辆)")
    total_credit = Column(Float, nullable=False, comment="总积分(分)")
    annual_output = Column(Integer, nullable=False, comment="年产量(辆)")
    status = Column(Enum(CreditRecordStatus), default=CreditRecordStatus.CALCULATED, nullable=False)
    calculated_at = Column(DateTime, default=datetime.utcnow)
    publicized_at = Column(DateTime)
    confirmed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vehicle_model = relationship("VehicleModel", back_populates="credit_records")


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    id = Column(Integer, primary_key=True, index=True)
    transaction_no = Column(String(50), unique=True, nullable=False, index=True)
    from_enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    to_enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    sell_order_id = Column(Integer, ForeignKey("credit_orders.id"), comment="卖单ID")
    buy_order_id = Column(Integer, ForeignKey("credit_orders.id"), comment="买单ID")
    credit_amount = Column(Float, nullable=False, comment="交易积分数量")
    unit_price = Column(Float, comment="交易单价(元/分)")
    total_amount = Column(Float, comment="交易总额(元)")
    transaction_date = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="completed", comment="交易状态")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    from_enterprise = relationship(
        "Enterprise",
        foreign_keys=[from_enterprise_id],
        back_populates="credit_transactions_from"
    )
    to_enterprise = relationship(
        "Enterprise",
        foreign_keys=[to_enterprise_id],
        back_populates="credit_transactions_to"
    )
    sell_order = relationship(
        "CreditOrder",
        foreign_keys=[sell_order_id],
        back_populates="sell_transactions"
    )
    buy_order = relationship(
        "CreditOrder",
        foreign_keys=[buy_order_id],
        back_populates="buy_transactions"
    )


class CreditOrder(Base):
    __tablename__ = "credit_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="核算年度")
    order_type = Column(Enum(OrderType), nullable=False, comment="订单类型：sell/buy")
    unit_price = Column(Float, nullable=False, comment="报价单价(元/分)")
    total_amount = Column(Float, nullable=False, comment="挂单总积分数量")
    filled_amount = Column(Float, default=0.0, comment="已成交积分数量")
    remaining_amount = Column(Float, nullable=False, comment="剩余积分数量")
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False, comment="订单状态")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = Column(DateTime, comment="过期时间")

    enterprise = relationship("Enterprise", back_populates="credit_orders")
    sell_transactions = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.sell_order_id",
        back_populates="sell_order"
    )
    buy_transactions = relationship(
        "CreditTransaction",
        foreign_keys="CreditTransaction.buy_order_id",
        back_populates="buy_order"
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    year = Column(Integer, nullable=False, comment="年度")
    trade_date = Column(DateTime, default=datetime.utcnow, comment="交易日期")
    unit_price = Column(Float, nullable=False, comment="成交单价(元/分)")
    credit_amount = Column(Float, nullable=False, comment="成交积分数量")
    total_amount = Column(Float, nullable=False, comment="成交总额(元)")
    from_enterprise_id = Column(Integer, ForeignKey("enterprises.id"))
    to_enterprise_id = Column(Integer, ForeignKey("enterprises.id"))
    transaction_id = Column(Integer, ForeignKey("credit_transactions.id"))
    created_at = Column(DateTime, default=datetime.utcnow)


class CreditCarryover(Base):
    __tablename__ = "credit_carryovers"

    id = Column(Integer, primary_key=True, index=True)
    carryover_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    from_year = Column(Integer, nullable=False, comment="结转来源年度")
    to_year = Column(Integer, nullable=False, comment="结转目标年度")
    original_amount = Column(Float, nullable=False, comment="原始正积分结余")
    carryover_ratio = Column(Float, nullable=False, comment="结转比例")
    carryover_amount = Column(Float, nullable=False, comment="实际结转积分数量")
    used_amount = Column(Float, default=0.0, comment="已使用结转积分数量")
    remaining_amount = Column(Float, nullable=False, comment="剩余结转积分数量")
    status = Column(Enum(CarryoverStatus), default=CarryoverStatus.APPROVED, nullable=False, comment="结转状态")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime)

    enterprise = relationship("Enterprise", back_populates="credit_carryovers_from")


class AnnualCreditSummary(Base):
    __tablename__ = "annual_credit_summaries"

    id = Column(Integer, primary_key=True, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False)
    year = Column(Integer, nullable=False, comment="年度")
    total_positive_credit = Column(Float, default=0.0, comment="当年正积分")
    total_negative_credit = Column(Float, default=0.0, comment="当年负积分")
    net_credit = Column(Float, default=0.0, comment="当年净积分")
    carryover_in = Column(Float, default=0.0, comment="上年结转积分")
    carryover_out = Column(Float, default=0.0, comment="结转下年积分")
    bought_credit = Column(Float, default=0.0, comment="买入积分")
    sold_credit = Column(Float, default=0.0, comment="卖出积分")
    final_net_credit = Column(Float, default=0.0, comment="最终净积分")
    credit_gap = Column(Float, default=0.0, comment="最终积分缺口")
    credit_surplus = Column(Float, default=0.0, comment="最终积分钟余")
    is_compliant = Column(Boolean, default=True, comment="是否达标")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        {'sqlite_autoincrement': True},
    )


class CreditBatch(Base):
    """积分批次：企业持有的每一批可追踪积分。

    数量守恒（对任一批次，忽略浮点误差后恒成立）：
        original_amount = remaining_amount + frozen_amount
                          + consumed_amount + expired_amount
    其中 remaining 为可用，frozen 为预留给未完成交易的数量。
    """
    __tablename__ = "credit_batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    source_year = Column(Integer, nullable=False, comment="来源年度（核发年度或购入/结转发生年度）")
    origin_year = Column(Integer, nullable=False, comment="最初核发年度，沿来源链追溯")
    acquisition_method = Column(Enum(BatchAcquisitionMethod), nullable=False, comment="取得方式")
    original_amount = Column(Float, nullable=False, comment="批次原始数量")
    remaining_amount = Column(Float, nullable=False, default=0.0, comment="可用（未冻结/未消耗/未过期）数量")
    frozen_amount = Column(Float, nullable=False, default=0.0, comment="预留给未完成交易的数量")
    consumed_amount = Column(Float, nullable=False, default=0.0, comment="已消耗（履约/出售/结转）数量")
    expired_amount = Column(Float, nullable=False, default=0.0, comment="已过期数量")
    valid_from = Column(DateTime, nullable=False, comment="适用期限起")
    valid_until = Column(DateTime, nullable=False, comment="适用期限止")
    status = Column(Enum(BatchStatus), default=BatchStatus.ACTIVE, nullable=False)
    rule_version = Column(String(20), nullable=False, default="v1", comment="选批规则版本")
    origin_ref_type = Column(String(30), comment="来源单据类型：credit_record/transaction/carryover")
    origin_ref_id = Column(Integer, comment="来源单据ID")
    parent_batch_id = Column(Integer, ForeignKey("credit_batches.id"), nullable=True, comment="退回批次的直接父批次")
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    parent_batch = relationship("CreditBatch", remote_side=[id], foreign_keys=[parent_batch_id])
    # 本批次作为「子」时的链（指向其父亲）；本批次作为「父」时的链（指向其派生子女）
    links_as_child = relationship(
        "CreditBatchLink", foreign_keys="CreditBatchLink.child_batch_id", back_populates="child_batch"
    )
    links_as_parent = relationship(
        "CreditBatchLink", foreign_keys="CreditBatchLink.parent_batch_id", back_populates="parent_batch"
    )


class CreditBatchLink(Base):
    """批次来源链：结转形成的新批次（child）由哪些父批次按多少数量构成。"""
    __tablename__ = "credit_batch_links"

    id = Column(Integer, primary_key=True, index=True)
    parent_batch_id = Column(Integer, ForeignKey("credit_batches.id"), nullable=False, index=True)
    child_batch_id = Column(Integer, ForeignKey("credit_batches.id"), nullable=False, index=True)
    parent_amount = Column(Float, nullable=False, comment="父批次出库数量（结转前）")
    child_amount = Column(Float, nullable=False, comment="分摊到子批次的数量（结转后）")
    carryover_ratio = Column(Float, nullable=False, comment="该段结转比例")
    carryover_id = Column(Integer, ForeignKey("credit_carryovers.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    parent_batch = relationship(
        "CreditBatch", foreign_keys=[parent_batch_id], back_populates="links_as_child"
    )
    child_batch = relationship(
        "CreditBatch", foreign_keys=[child_batch_id], back_populates="links_as_parent"
    )


class CreditBatchSelectionGroup(Base):
    """一次消费/预留的选批结果与理由（可解释、可按版本重放）。"""
    __tablename__ = "credit_batch_selection_groups"

    id = Column(Integer, primary_key=True, index=True)
    group_no = Column(String(50), unique=True, nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    purpose = Column(Enum(AllocationPurpose), nullable=False)
    amount = Column(Float, nullable=False, comment="需求数量")
    rule_version = Column(String(20), nullable=False)
    ref_type = Column(String(30), comment="关联单据类型：order/transaction/carryover/fulfillment")
    ref_id = Column(Integer, comment="关联单据ID")
    ref_no = Column(String(50), comment="关联单据号")
    candidates_snapshot = Column(Text, comment="选批前候选批次快照(JSON)")
    reason_summary = Column(Text, comment="选择理由汇总")
    replayed = Column(Boolean, default=False, comment="是否为历史重放产生")
    created_at = Column(DateTime, default=datetime.utcnow)

    allocations = relationship(
        "CreditBatchAllocation", back_populates="group", cascade="all, delete-orphan"
    )


class CreditBatchAllocation(Base):
    """批次分配行：某个批次向某次消费贡献了多少数量，以及该行当前状态。

    reserve（挂单预留）：held；成交后转 consumed；撤单 released。
    fulfillment/sell/carryover：直接 consumed。
    退回：consumed 行标记 restored，同额恢复到批次（或新退回批次）。
    """
    __tablename__ = "credit_batch_allocations"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("credit_batch_selection_groups.id"), nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("credit_batches.id"), nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    purpose = Column(Enum(AllocationPurpose), nullable=False)
    amount = Column(Float, nullable=False, comment="本行数量")
    status = Column(Enum(AllocationStatus), default=AllocationStatus.HELD, nullable=False)
    reason = Column(String(500), comment="本行入选理由")
    consumed_amount = Column(Float, default=0.0, comment="已随成交消耗数量")
    released_amount = Column(Float, default=0.0, comment="撤单已释放数量")
    restored_amount = Column(Float, default=0.0, comment="已退回恢复数量")
    restore_batch_id = Column(Integer, ForeignKey("credit_batches.id"), nullable=True, comment="退回去向批次")
    order_id = Column(Integer, ForeignKey("credit_orders.id"), nullable=True, index=True)
    transaction_id = Column(Integer, ForeignKey("credit_transactions.id"), nullable=True, index=True)
    source_allocation_id = Column(Integer, ForeignKey("credit_batch_allocations.id"), nullable=True,
                                  comment="成交行对应的预留行")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    group = relationship("CreditBatchSelectionGroup", back_populates="allocations")
    batch = relationship("CreditBatch", foreign_keys=[batch_id])
    restore_batch = relationship("CreditBatch", foreign_keys=[restore_batch_id])


class CreditBatchLedgerEntry(Base):
    """批次台账流水：批次数量的每一次增减，供审计核对守恒。"""
    __tablename__ = "credit_batch_ledger_entries"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("credit_batches.id"), nullable=False, index=True)
    enterprise_id = Column(Integer, ForeignKey("enterprises.id"), nullable=False, index=True)
    entry_type = Column(String(30), nullable=False,
                        comment="类型：issue/reserve/release/consume/expire/restore/carryover_out/carryover_in")
    amount = Column(Float, nullable=False, comment="带符号数量：正为增加可用，负为减少可用")
    balance_after = Column(Float, nullable=False, comment="记账后批次可用余额")
    ref_type = Column(String(30))
    ref_id = Column(Integer)
    ref_no = Column(String(50))
    allocation_id = Column(Integer, ForeignKey("credit_batch_allocations.id"), nullable=True)
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
