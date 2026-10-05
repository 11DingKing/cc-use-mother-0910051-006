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
