"""积分批次台账服务。

设计要点：
- 企业余额拆分为可追踪批次（来源年度/取得方式/剩余量/适用期限）；
- 履约、出售、撤单、退回、结转、过期等每一次数量变动都写成
  BatchAllocation + BatchAllocationItem 台账，条目带正负 delta，
  批次当前状态恒等于其全部台账 delta 之和（数量守恒可独立重算）；
- 每次规则选择都记录规则版本、规则快照与候选快照，支持换版后按原规则重放；
- 过期处理只核销未预留部分，不动已预留给未完成交易的数量；
- 结转/购入形成的新批次通过 CreditBatchLineage 保留来源链。

本模块不 import crud（避免循环依赖）；所有函数不在内部 commit，
由调用方（crud 钩子或路由）统一提交。
"""
import json
from datetime import datetime
from typing import List, Optional, Dict, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func

from . import models
from .batch_rules import (
    BatchCandidate,
    InsufficientCreditsError,
    select_batches,
    walk_reservation_items,
    merge_rule_params,
    DEFAULT_RULE_VERSION,
    DEFAULT_RULE_NAME,
    DEFAULT_RULE_PARAMS,
    STRATEGY_EXPIRY_FIRST,
    METHOD_LABELS,
    EPSILON,
)
from .models import (
    AcquisitionMethod,
    CreditBatchStatus,
    AllocationPurpose,
    AllocationStatus,
    OrderType,
)

ROUND = lambda x: round(float(x), 2)  # noqa: E731


# ---------------------------------------------------------------------------
# 规则版本管理
# ---------------------------------------------------------------------------

def ensure_default_rule(db: Session) -> models.AllocationRule:
    rule = db.query(models.AllocationRule).filter(
        models.AllocationRule.version == DEFAULT_RULE_VERSION
    ).first()
    if rule:
        return rule
    has_active = db.query(models.AllocationRule).filter(
        models.AllocationRule.is_active == True  # noqa: E712
    ).first()
    rule = models.AllocationRule(
        version=DEFAULT_RULE_VERSION,
        name=DEFAULT_RULE_NAME,
        description="系统内置默认规则：先到期先核销，同到期按取得先后；核发3年、结转2年有效",
        strategy=STRATEGY_EXPIRY_FIRST,
        params=json.dumps(DEFAULT_RULE_PARAMS, ensure_ascii=False),
        is_active=has_active is None,
    )
    db.add(rule)
    db.flush()
    return rule


def get_active_rule(db: Session) -> models.AllocationRule:
    rule = db.query(models.AllocationRule).filter(
        models.AllocationRule.is_active == True  # noqa: E712
    ).first()
    if rule:
        return rule
    return ensure_default_rule(db)


def get_rule_by_version(db: Session, version: str) -> Optional[models.AllocationRule]:
    return db.query(models.AllocationRule).filter(
        models.AllocationRule.version == version
    ).first()


def get_rules(db: Session) -> List[models.AllocationRule]:
    ensure_default_rule(db)
    return db.query(models.AllocationRule).order_by(models.AllocationRule.id).all()


def create_rule_version(
    db: Session,
    version: str,
    name: str,
    strategy: str,
    params: Optional[Dict] = None,
    description: Optional[str] = None,
    activate: bool = False,
) -> models.AllocationRule:
    ensure_default_rule(db)
    if get_rule_by_version(db, version):
        raise ValueError(f"规则版本 {version} 已存在，规则版本不可变，请使用新版本号")
    merged = merge_rule_params(params)
    rule = models.AllocationRule(
        version=version,
        name=name,
        description=description,
        strategy=strategy,
        params=json.dumps(merged, ensure_ascii=False),
        is_active=False,
    )
    db.add(rule)
    db.flush()
    if activate:
        activate_rule(db, rule.id)
    return rule


def activate_rule(db: Session, rule_id: int) -> models.AllocationRule:
    rule = db.get(models.AllocationRule, rule_id)
    if not rule:
        raise ValueError("规则不存在")
    db.query(models.AllocationRule).update({"is_active": False})
    rule.is_active = True
    db.flush()
    return rule


def rule_params(rule: models.AllocationRule) -> Dict:
    try:
        return merge_rule_params(json.loads(rule.params or "{}"))
    except (TypeError, json.JSONDecodeError):
        return merge_rule_params(None)


def _rule_snapshot(rule: models.AllocationRule) -> str:
    return json.dumps({
        "version": rule.version,
        "name": rule.name,
        "strategy": rule.strategy,
        "params": rule_params(rule),
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 编号
# ---------------------------------------------------------------------------

def generate_batch_no(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    count = db.query(models.CreditBatch).filter(
        func.substr(models.CreditBatch.batch_no, 4, 8) == today
    ).count() + 1
    return f"BAT{today}{count:05d}"


def generate_allocation_no(db: Session) -> str:
    today = datetime.now().strftime("%Y%m%d")
    count = db.query(models.BatchAllocation).filter(
        func.substr(models.BatchAllocation.allocation_no, 3, 8) == today
    ).count() + 1
    return f"AL{today}{count:05d}"


# ---------------------------------------------------------------------------
# 批次基础操作
# ---------------------------------------------------------------------------

def _has_any_batches(db: Session, enterprise_id: int) -> bool:
    """企业是否已有批次台账。无台账的历史数据走旧逻辑（兼容存量流程）。"""
    return db.query(models.CreditBatch.id).filter(
        models.CreditBatch.enterprise_id == enterprise_id
    ).first() is not None


def _refresh_batch_status(batch: models.CreditBatch) -> None:
    if batch.remaining_amount > EPSILON or batch.reserved_amount > EPSILON:
        batch.status = CreditBatchStatus.ACTIVE
    elif batch.expired_amount > EPSILON:
        batch.status = CreditBatchStatus.EXPIRED
    else:
        batch.status = CreditBatchStatus.DEPLETED


def _assert_batch_consistent(batch: models.CreditBatch) -> None:
    for field_name in ("initial_amount", "remaining_amount", "reserved_amount",
                       "consumed_amount", "expired_amount"):
        if getattr(batch, field_name) < -EPSILON:
            raise ValueError(
                f"批次{batch.batch_no}数量守恒被破坏：{field_name}="
                f"{getattr(batch, field_name)} 出现负值"
            )
    total = ROUND(batch.remaining_amount + batch.reserved_amount
                  + batch.consumed_amount + batch.expired_amount)
    if abs(total - ROUND(batch.initial_amount)) > 0.02:
        raise ValueError(
            f"批次{batch.batch_no}数量守恒被破坏：初始{batch.initial_amount} != "
            f"剩余{batch.remaining_amount}+预留{batch.reserved_amount}"
            f"+消耗{batch.consumed_amount}+过期{batch.expired_amount}"
        )


def _create_batch(
    db: Session,
    enterprise_id: int,
    method: AcquisitionMethod,
    source_year: int,
    acquired_year: int,
    expiry_year: int,
    source_type: Optional[str] = None,
    source_id: Optional[int] = None,
) -> models.CreditBatch:
    """创建空批次（各数量字段为0），由台账条目注入数量，保证台账即真相。"""
    batch = models.CreditBatch(
        batch_no=generate_batch_no(db),
        enterprise_id=enterprise_id,
        source_year=source_year,
        acquired_year=acquired_year,
        acquisition_method=method,
        initial_amount=0.0,
        remaining_amount=0.0,
        reserved_amount=0.0,
        consumed_amount=0.0,
        expired_amount=0.0,
        expiry_year=expiry_year,
        status=CreditBatchStatus.ACTIVE,
        source_type=source_type,
        source_id=source_id,
    )
    db.add(batch)
    db.flush()
    return batch


def _create_allocation(
    db: Session,
    enterprise_id: int,
    purpose: AllocationPurpose,
    year: int,
    total_amount: float,
    explanation: str,
    rule: Optional[models.AllocationRule] = None,
    candidates_snapshot: Optional[str] = None,
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
    reverses_allocation_id: Optional[int] = None,
) -> models.BatchAllocation:
    allocation = models.BatchAllocation(
        allocation_no=generate_allocation_no(db),
        enterprise_id=enterprise_id,
        purpose=purpose,
        year=year,
        total_amount=ROUND(total_amount),
        rule_version=rule.version if rule else None,
        rule_snapshot=_rule_snapshot(rule) if rule else None,
        candidates_snapshot=candidates_snapshot,
        status=AllocationStatus.ACTIVE,
        reference_type=reference_type,
        reference_id=reference_id,
        reverses_allocation_id=reverses_allocation_id,
        explanation=explanation,
    )
    db.add(allocation)
    db.flush()
    return allocation


def _add_item(
    db: Session,
    allocation: models.BatchAllocation,
    batch: models.CreditBatch,
    amount: float,
    reason: str,
    *,
    d_initial: float = 0.0,
    d_remaining: float = 0.0,
    d_reserved: float = 0.0,
    d_consumed: float = 0.0,
    d_expired: float = 0.0,
) -> models.BatchAllocationItem:
    """应用一条批次数量变动并落台账。所有批次字段变动都必须经过这里。"""
    amount = ROUND(amount)
    before = ROUND(batch.remaining_amount)
    batch.initial_amount = ROUND(batch.initial_amount + d_initial)
    batch.remaining_amount = ROUND(batch.remaining_amount + d_remaining)
    batch.reserved_amount = ROUND(batch.reserved_amount + d_reserved)
    batch.consumed_amount = ROUND(batch.consumed_amount + d_consumed)
    batch.expired_amount = ROUND(batch.expired_amount + d_expired)
    batch.updated_at = datetime.utcnow()
    _assert_batch_consistent(batch)
    _refresh_batch_status(batch)

    item = models.BatchAllocationItem(
        allocation_id=allocation.id,
        batch_id=batch.id,
        amount=amount,
        delta_initial=ROUND(d_initial),
        delta_remaining=ROUND(d_remaining),
        delta_reserved=ROUND(d_reserved),
        delta_consumed=ROUND(d_consumed),
        delta_expired=ROUND(d_expired),
        remaining_before=before,
        remaining_after=ROUND(batch.remaining_amount),
        reason=reason,
    )
    db.add(item)
    db.flush()
    return item


def _validity_expiry(params: Dict, method: str, acquired_year: int) -> int:
    validity = (params.get("validity_years") or {}).get(method)
    if validity is None:
        validity = DEFAULT_RULE_PARAMS["validity_years"].get(method)
    if validity is None:
        validity = 3
    return acquired_year + int(validity) - 1


def _load_candidates(
    db: Session,
    enterprise_id: int,
    params: Dict,
    purpose_key: str,
) -> List[BatchCandidate]:
    eligible = (params.get("eligible_sources") or {}).get(purpose_key)
    query = db.query(models.CreditBatch).filter(
        models.CreditBatch.enterprise_id == enterprise_id,
        models.CreditBatch.status == CreditBatchStatus.ACTIVE,
        models.CreditBatch.remaining_amount > EPSILON,
    )
    if eligible:
        methods = [AcquisitionMethod(m) for m in eligible]
        query = query.filter(models.CreditBatch.acquisition_method.in_(methods))
    batches = query.order_by(models.CreditBatch.id).all()
    return [
        BatchCandidate(
            batch_id=b.id,
            batch_no=b.batch_no,
            available=ROUND(b.remaining_amount),
            acquisition_method=b.acquisition_method.value,
            source_year=b.source_year,
            acquired_year=b.acquired_year,
            expiry_year=b.expiry_year,
        )
        for b in batches
    ]


def _candidates_snapshot(candidates: List[BatchCandidate]) -> str:
    return json.dumps([
        {
            "batch_id": c.batch_id,
            "batch_no": c.batch_no,
            "available": c.available,
            "acquisition_method": c.acquisition_method,
            "source_year": c.source_year,
            "acquired_year": c.acquired_year,
            "expiry_year": c.expiry_year,
        }
        for c in candidates
    ], ensure_ascii=False)


def _select_and_apply(
    db: Session,
    enterprise_id: int,
    amount: float,
    purpose: AllocationPurpose,
    purpose_key: str,
    year: int,
    reference_type: Optional[str],
    reference_id: Optional[int],
    delta_mode: str,
) -> models.BatchAllocation:
    """按当前规则选择批次并应用数量变动，返回台账分配记录。

    delta_mode: "reserve"（剩余→预留）/ "consume"（剩余→消耗）/ "expire"（剩余→过期）
    """
    rule = get_active_rule(db)
    params = rule_params(rule)
    candidates = _load_candidates(db, enterprise_id, params, purpose_key)
    result = select_batches(
        candidates,
        amount,
        strategy=rule.strategy,
        source_priority=params.get("source_priority"),
        eligible_methods=(params.get("eligible_sources") or {}).get(purpose_key),
        purpose=purpose_key,
        rule_version=rule.version,
    )
    allocation = _create_allocation(
        db,
        enterprise_id=enterprise_id,
        purpose=purpose,
        year=year,
        total_amount=amount,
        explanation=result.explanation,
        rule=rule,
        candidates_snapshot=json.dumps({
            "candidates": json.loads(_candidates_snapshot(candidates)),
        }, ensure_ascii=False),
        reference_type=reference_type,
        reference_id=reference_id,
    )
    for selected in result.items:
        batch = db.get(models.CreditBatch, selected.batch_id)
        if delta_mode == "reserve":
            _add_item(db, allocation, batch, selected.amount, selected.reason,
                      d_remaining=-selected.amount, d_reserved=selected.amount)
        elif delta_mode == "expire":
            _add_item(db, allocation, batch, selected.amount, selected.reason,
                      d_remaining=-selected.amount, d_expired=selected.amount)
        else:
            _add_item(db, allocation, batch, selected.amount, selected.reason,
                      d_remaining=-selected.amount, d_consumed=selected.amount)
    return allocation


# ---------------------------------------------------------------------------
# 核发入账
# ---------------------------------------------------------------------------

def create_issued_batch_for_record(
    db: Session, record: models.CreditRecord
) -> Optional[models.CreditBatch]:
    """积分记录确认后，正积分按批次核发入账（幂等）。"""
    if record.total_credit <= EPSILON:
        return None
    existing = db.query(models.CreditBatch).filter(
        models.CreditBatch.source_type == "credit_record",
        models.CreditBatch.source_id == record.id,
    ).first()
    if existing:
        return existing

    vehicle_model = db.get(models.VehicleModel, record.vehicle_model_id)
    if not vehicle_model:
        return None
    enterprise_id = vehicle_model.enterprise_id

    rule = get_active_rule(db)
    params = rule_params(rule)
    expiry_year = _validity_expiry(params, AcquisitionMethod.ISSUED.value, record.year)

    batch = _create_batch(
        db,
        enterprise_id=enterprise_id,
        method=AcquisitionMethod.ISSUED,
        source_year=record.year,
        acquired_year=record.year,
        expiry_year=expiry_year,
        source_type="credit_record",
        source_id=record.id,
    )
    allocation = _create_allocation(
        db,
        enterprise_id=enterprise_id,
        purpose=AllocationPurpose.ISSUE,
        year=record.year,
        total_amount=record.total_credit,
        explanation=(
            f"{record.year}年度核算记录确认，核发正积分{ROUND(record.total_credit)}分入账，"
            f"适用期限至{expiry_year}年末"
        ),
        rule=rule,
        reference_type="credit_record",
        reference_id=record.id,
    )
    _add_item(
        db, allocation, batch, record.total_credit,
        f"{record.year}年度核发入账，适用期限至{expiry_year}年末",
        d_initial=record.total_credit,
        d_remaining=record.total_credit,
    )
    return batch


def sync_issued_batches(db: Session) -> int:
    """为已确认但尚未建批次的积分记录补建核发批次（存量数据迁移）。"""
    records = db.query(models.CreditRecord).filter(
        models.CreditRecord.status == models.CreditRecordStatus.CONFIRMED,
        models.CreditRecord.total_credit > EPSILON,
    ).all()
    created = 0
    for record in records:
        if create_issued_batch_for_record(db, record) is not None:
            created += 1
    return created


# ---------------------------------------------------------------------------
# 出售：挂单预留 → 成交核销 / 撤单释放
# ---------------------------------------------------------------------------

def reserve_for_order(
    db: Session,
    order: models.CreditOrder,
    amount: Optional[float] = None,
) -> Optional[models.BatchAllocation]:
    """卖单挂单时按规则预留批次（剩余量→预留量）。"""
    if order.order_type != OrderType.SELL:
        return None
    reserve_amount = ROUND(amount if amount is not None else order.remaining_amount)
    if reserve_amount <= EPSILON:
        return None
    if not _has_any_batches(db, order.enterprise_id):
        return None  # 存量无台账数据，走旧逻辑
    return _select_and_apply(
        db,
        enterprise_id=order.enterprise_id,
        amount=reserve_amount,
        purpose=AllocationPurpose.RESERVE,
        purpose_key="reserve",
        year=order.year,
        reference_type="credit_order",
        reference_id=order.id,
        delta_mode="reserve",
    )


def _active_reserve_items(db: Session, order_id: int) -> List[Tuple[models.BatchAllocationItem, models.CreditBatch]]:
    """卖单当前仍有效的预留明细（按预留先后排序）。"""
    allocations = db.query(models.BatchAllocation).filter(
        models.BatchAllocation.reference_type == "credit_order",
        models.BatchAllocation.reference_id == order_id,
        models.BatchAllocation.purpose == AllocationPurpose.RESERVE,
        models.BatchAllocation.status == AllocationStatus.ACTIVE,
    ).order_by(models.BatchAllocation.id).all()
    pairs = []
    for allocation in allocations:
        for item in allocation.items:
            outstanding = ROUND(item.amount - item.consumed_amount - item.released_amount)
            if outstanding > EPSILON:
                pairs.append((item, item.batch))
    return pairs


def release_for_order(
    db: Session,
    order: models.CreditOrder,
    amount: Optional[float] = None,
) -> Optional[models.BatchAllocation]:
    """撤单/减量时按预留记录原路释放（预留量→剩余量）。

    释放时若批次已过适用期限，释放量不回到可用池，直接过期核销，
    并单独落 EXPIRE 台账，保证过程可解释、数量仍守恒。
    """
    pairs = _active_reserve_items(db, order.id)
    if not pairs:
        return None
    outstanding_total = ROUND(sum(
        item.amount - item.consumed_amount - item.released_amount for item, _ in pairs
    ))
    release_amount = outstanding_total if amount is None else min(ROUND(amount), outstanding_total)
    if release_amount <= EPSILON:
        return None

    allocation = _create_allocation(
        db,
        enterprise_id=order.enterprise_id,
        purpose=AllocationPurpose.RELEASE,
        year=order.year,
        total_amount=release_amount,
        explanation=(
            f"订单{order.order_no}撤单/减量，按预留记录原路释放{release_amount}分"
            f"（共{len(pairs)}条预留明细）"
        ),
        reference_type="credit_order",
        reference_id=order.id,
    )

    need = release_amount
    released_by_batch: Dict[int, float] = {}
    for item, batch in pairs:
        if need <= EPSILON:
            break
        outstanding = ROUND(item.amount - item.consumed_amount - item.released_amount)
        take = ROUND(min(outstanding, need))
        if take <= EPSILON:
            continue
        item.released_amount = ROUND(item.released_amount + take)
        _add_item(
            db, allocation, batch, take,
            f"撤单释放：批次{batch.batch_no}预留退回可用量",
            d_reserved=-take,
            d_remaining=take,
        )
        released_by_batch[batch.id] = ROUND(released_by_batch.get(batch.id, 0.0) + take)
        need = ROUND(need - take)

    _expire_restored_if_due(db, order.enterprise_id, released_by_batch, order.year)
    return allocation


def _expire_restored_if_due(
    db: Session,
    enterprise_id: int,
    amounts_by_batch: Dict[int, float],
    year: int,
) -> Optional[models.BatchAllocation]:
    """释放/退回恢复的数量若落在已过适用期限的批次上，立即过期核销。"""
    current_year = datetime.now().year
    due = []
    for batch_id, amount in amounts_by_batch.items():
        batch = db.get(models.CreditBatch, batch_id)
        if batch and batch.expiry_year < current_year and batch.remaining_amount > EPSILON:
            expire_amount = ROUND(min(amount, batch.remaining_amount))
            if expire_amount > EPSILON:
                due.append((batch, expire_amount))
    if not due:
        return None

    allocation = _create_allocation(
        db,
        enterprise_id=enterprise_id,
        purpose=AllocationPurpose.EXPIRE,
        year=year,
        total_amount=ROUND(sum(a for _, a in due)),
        explanation="释放/退回的数量落在已过适用期限的批次上，恢复同时直接过期核销",
        reference_type="credit_order",
    )
    for batch, expire_amount in due:
        _add_item(
            db, allocation, batch, expire_amount,
            f"批次{batch.batch_no}适用期限至{batch.expiry_year}年末，恢复量直接核销",
            d_remaining=-expire_amount,
            d_expired=expire_amount,
        )
    return allocation


def consume_for_sale(
    db: Session,
    transaction: models.CreditTransaction,
    amount: float,
    year: int,
    sell_order: Optional[models.CreditOrder] = None,
) -> Optional[models.BatchAllocation]:
    """成交核销：优先按该卖单的预留明细原路核销（预留量→消耗量），
    无预留（存量订单/直接交易）时按规则从可用批次选择（剩余量→消耗量）。
    同时为买方按来源批次逐条建立购入批次，继承来源年度与适用期限并保留来源链。
    """
    seller_id = transaction.from_enterprise_id
    buyer_id = transaction.to_enterprise_id
    amount = ROUND(amount)

    existing = db.query(models.BatchAllocation).filter(
        models.BatchAllocation.reference_type == "credit_transaction",
        models.BatchAllocation.reference_id == transaction.id,
        models.BatchAllocation.purpose == AllocationPurpose.SALE,
    ).first()
    if existing:
        return existing  # 幂等：同一笔交易只核销一次

    sale_items: List[Tuple[models.CreditBatch, float, str]] = []
    reserve_snapshot: List[Dict] = []
    need = amount

    # 1) 预留原路核销（顺序与快照一致，重放结果相同）
    if sell_order is not None:
        pairs = _active_reserve_items(db, sell_order.id)
        if pairs:
            reserve_snapshot = [
                {
                    "batch_id": item.batch_id,
                    "batch_no": batch.batch_no,
                    "outstanding": ROUND(item.amount - item.consumed_amount - item.released_amount),
                }
                for item, batch in pairs
            ]
            for item, batch in pairs:
                if need <= EPSILON:
                    break
                outstanding = ROUND(item.amount - item.consumed_amount - item.released_amount)
                take = ROUND(min(outstanding, need))
                if take <= EPSILON:
                    continue
                item.consumed_amount = ROUND(item.consumed_amount + take)
                sale_items.append((batch, take,
                                   f"订单{sell_order.order_no}成交，按预留记录核销批次{batch.batch_no}"))
                need = ROUND(need - take)

    # 2) 剩余部分按规则直选（存量无预留订单或直接交易）
    selection = None
    candidates = []
    if need > EPSILON:
        if not _has_any_batches(db, seller_id):
            if sale_items:
                raise InsufficientCreditsError(
                    f"卖方批次台账不足：成交{amount}分，预留仅覆盖{ROUND(amount - need)}分"
                )
            return None  # 存量无台账数据，走旧逻辑
        rule = get_active_rule(db)
        params = rule_params(rule)
        candidates = _load_candidates(db, seller_id, params, "sale")
        selection = select_batches(
            candidates,
            need,
            strategy=rule.strategy,
            source_priority=params.get("source_priority"),
            eligible_methods=(params.get("eligible_sources") or {}).get("sale"),
            purpose="sale",
            rule_version=rule.version,
        )

    rule = get_active_rule(db)
    explanation_parts = []
    if sale_items:
        explanation_parts.append(
            f"按订单预留记录核销{ROUND(amount - need)}分（{len(sale_items)}条预留明细）"
        )
    if selection:
        explanation_parts.append(selection.explanation)
    allocation = _create_allocation(
        db,
        enterprise_id=seller_id,
        purpose=AllocationPurpose.SALE,
        year=year,
        total_amount=amount,
        explanation="；".join(explanation_parts) or f"出售核销{amount}分",
        rule=rule,
        candidates_snapshot=json.dumps({
            "reserve_items": reserve_snapshot,
            "candidates": [
                {
                    "batch_id": c.batch_id,
                    "batch_no": c.batch_no,
                    "available": c.available,
                    "acquisition_method": c.acquisition_method,
                    "source_year": c.source_year,
                    "acquired_year": c.acquired_year,
                    "expiry_year": c.expiry_year,
                }
                for c in candidates
            ],
        }, ensure_ascii=False),
        reference_type="credit_transaction",
        reference_id=transaction.id,
    )

    for batch, take, reason in sale_items:
        _add_item(db, allocation, batch, take, reason,
                  d_reserved=-take, d_consumed=take)
    if selection:
        for selected in selection.items:
            batch = db.get(models.CreditBatch, selected.batch_id)
            _add_item(db, allocation, batch, selected.amount, selected.reason,
                      d_remaining=-selected.amount, d_consumed=selected.amount)

    # 3) 买方购入批次：逐来源批次建立，继承来源年度与适用期限，保留来源链
    purchase_allocation = _create_allocation(
        db,
        enterprise_id=buyer_id,
        purpose=AllocationPurpose.PURCHASE,
        year=year,
        total_amount=amount,
        explanation=f"自交易{transaction.transaction_no}购入{amount}分，按卖方批次继承来源年度与适用期限",
        rule=rule,
        reference_type="credit_transaction",
        reference_id=transaction.id,
    )
    for item in allocation.items:
        seller_batch = db.get(models.CreditBatch, item.batch_id)
        buyer_batch = _create_batch(
            db,
            enterprise_id=buyer_id,
            method=AcquisitionMethod.PURCHASED,
            source_year=seller_batch.source_year,
            acquired_year=year,
            expiry_year=seller_batch.expiry_year,
            source_type="credit_transaction",
            source_id=transaction.id,
        )
        db.add(models.CreditBatchLineage(
            child_batch_id=buyer_batch.id,
            parent_batch_id=seller_batch.id,
            amount=item.amount,
        ))
        _add_item(
            db, purchase_allocation, buyer_batch, item.amount,
            f"市场购入：源自卖方批次{seller_batch.batch_no}"
            f"（{seller_batch.source_year}年来源，期限至{seller_batch.expiry_year}年末）",
            d_initial=item.amount,
            d_remaining=item.amount,
        )
    return allocation


# ---------------------------------------------------------------------------
# 履约与退回
# ---------------------------------------------------------------------------

def fulfill_compliance(
    db: Session,
    enterprise_id: int,
    year: int,
    amount: float,
) -> models.BatchAllocation:
    """履约核销：按规则从可用批次选择（剩余量→消耗量）。"""
    return _select_and_apply(
        db,
        enterprise_id=enterprise_id,
        amount=amount,
        purpose=AllocationPurpose.COMPLIANCE,
        purpose_key="compliance",
        year=year,
        reference_type="compliance",
        reference_id=None,
        delta_mode="consume",
    )


def consume_compliance_from_batch(
    db: Session,
    batch: models.CreditBatch,
    amount: float,
    year: int,
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
) -> Optional[models.BatchAllocation]:
    """定向履约：指定批次直接用于抵偿缺口（如结转积分落地即抵偿）。"""
    amount = ROUND(amount)
    if amount <= EPSILON:
        return None
    if batch.remaining_amount < amount - 0.001:
        raise InsufficientCreditsError(
            f"批次{batch.batch_no}可用量不足：需{amount}分，仅剩{ROUND(batch.remaining_amount)}分"
        )
    allocation = _create_allocation(
        db,
        enterprise_id=batch.enterprise_id,
        purpose=AllocationPurpose.COMPLIANCE,
        year=year,
        total_amount=amount,
        explanation=f"批次{batch.batch_no}定向用于抵偿{year}年度积分缺口{amount}分",
        reference_type=reference_type,
        reference_id=reference_id,
    )
    _add_item(db, allocation, batch, amount,
              f"批次{batch.batch_no}定向履约核销",
              d_remaining=-amount, d_consumed=amount)
    return allocation


def reverse_allocation(
    db: Session,
    allocation_id: int,
    reason: Optional[str] = None,
) -> List[models.BatchAllocation]:
    """退回/回退：按台账明细原路恢复，全程数量守恒。

    - COMPLIANCE：消耗量退回剩余量（过期批次上的恢复量直接核销）；
    - SALE：卖方批次恢复，同时追回买方购入批次（买方已用则拒绝整体退回）。
    """
    allocation = db.get(models.BatchAllocation, allocation_id)
    if not allocation:
        raise ValueError("分配记录不存在")
    if allocation.status == AllocationStatus.REVERSED:
        raise ValueError("该分配已回退，不能重复回退")
    if allocation.purpose not in (AllocationPurpose.COMPLIANCE, AllocationPurpose.SALE):
        raise ValueError(f"{allocation.purpose.value} 类型的分配不支持回退")

    reason_text = f"：{reason}" if reason else ""
    created: List[models.BatchAllocation] = []

    if allocation.purpose == AllocationPurpose.COMPLIANCE:
        return_allocation = _create_allocation(
            db,
            enterprise_id=allocation.enterprise_id,
            purpose=AllocationPurpose.RETURN,
            year=allocation.year,
            total_amount=allocation.total_amount,
            explanation=f"履约退回，按原分配{allocation.allocation_no}明细原路恢复{reason_text}",
            reference_type="reversal",
            reference_id=allocation.id,
            reverses_allocation_id=allocation.id,
        )
        restored: Dict[int, float] = {}
        for item in allocation.items:
            batch = db.get(models.CreditBatch, item.batch_id)
            _add_item(db, return_allocation, batch, item.amount,
                      f"履约退回：恢复批次{batch.batch_no}可用量",
                      d_consumed=-item.amount, d_remaining=item.amount)
            restored[batch.id] = ROUND(restored.get(batch.id, 0.0) + item.amount)
        expire_allocation = _expire_restored_if_due(
            db, allocation.enterprise_id, restored, allocation.year)
        created.append(return_allocation)
        if expire_allocation:
            created.append(expire_allocation)

    else:  # SALE
        transaction_id = allocation.reference_id
        purchase_allocation = db.query(models.BatchAllocation).filter(
            models.BatchAllocation.reference_type == "credit_transaction",
            models.BatchAllocation.reference_id == transaction_id,
            models.BatchAllocation.purpose == AllocationPurpose.PURCHASE,
            models.BatchAllocation.status == AllocationStatus.ACTIVE,
        ).first()

        # 先校验后落账：买方批次必须仍持有足够数量才能整体退回
        if purchase_allocation:
            for item in purchase_allocation.items:
                buyer_batch = db.get(models.CreditBatch, item.batch_id)
                if buyer_batch.remaining_amount < item.amount - 0.001:
                    raise ValueError(
                        f"买方批次{buyer_batch.batch_no}的购入量已被使用"
                        f"（剩余{ROUND(buyer_batch.remaining_amount)}分 < 购入{item.amount}分），"
                        f"无法整体退回该交易"
                    )

        return_allocation = _create_allocation(
            db,
            enterprise_id=allocation.enterprise_id,
            purpose=AllocationPurpose.RETURN,
            year=allocation.year,
            total_amount=allocation.total_amount,
            explanation=f"出售退回，按原分配{allocation.allocation_no}明细恢复卖方批次{reason_text}",
            reference_type="reversal",
            reference_id=allocation.id,
            reverses_allocation_id=allocation.id,
        )
        restored = {}
        for item in allocation.items:
            batch = db.get(models.CreditBatch, item.batch_id)
            _add_item(db, return_allocation, batch, item.amount,
                      f"出售退回：恢复批次{batch.batch_no}可用量",
                      d_consumed=-item.amount, d_remaining=item.amount)
            restored[batch.id] = ROUND(restored.get(batch.id, 0.0) + item.amount)
        created.append(return_allocation)

        if purchase_allocation:
            clawback_allocation = _create_allocation(
                db,
                enterprise_id=purchase_allocation.enterprise_id,
                purpose=AllocationPurpose.CLAWBACK,
                year=allocation.year,
                total_amount=purchase_allocation.total_amount,
                explanation=f"交易退回，追回买方购入批次{reason_text}",
                reference_type="reversal",
                reference_id=allocation.id,
                reverses_allocation_id=allocation.id,
            )
            for item in purchase_allocation.items:
                buyer_batch = db.get(models.CreditBatch, item.batch_id)
                _add_item(db, clawback_allocation, buyer_batch, item.amount,
                          f"交易退回：购入批次{buyer_batch.batch_no}追回注销",
                          d_initial=-item.amount, d_remaining=-item.amount)
            purchase_allocation.status = AllocationStatus.REVERSED
            purchase_allocation.reversed_at = datetime.utcnow()
            created.append(clawback_allocation)

        expire_allocation = _expire_restored_if_due(
            db, allocation.enterprise_id, restored, allocation.year)
        if expire_allocation:
            created.append(expire_allocation)

        transaction = db.get(models.CreditTransaction, transaction_id)
        if transaction:
            transaction.status = "returned"

    allocation.status = AllocationStatus.REVERSED
    allocation.reversed_at = datetime.utcnow()
    return created


def return_transaction(
    db: Session, transaction_id: int, reason: Optional[str] = None
) -> List[models.BatchAllocation]:
    """退回一笔交易对应的全部出售核销（卖方恢复 + 买方追回）。"""
    sale_allocations = db.query(models.BatchAllocation).filter(
        models.BatchAllocation.reference_type == "credit_transaction",
        models.BatchAllocation.reference_id == transaction_id,
        models.BatchAllocation.purpose == AllocationPurpose.SALE,
        models.BatchAllocation.status == AllocationStatus.ACTIVE,
    ).all()
    if not sale_allocations:
        raise ValueError("该交易没有可退回的出售核销记录")
    created: List[models.BatchAllocation] = []
    for allocation in sale_allocations:
        created.extend(reverse_allocation(db, allocation.id, reason))
    return created


# ---------------------------------------------------------------------------
# 结转：转出消耗 + 新批次（保留来源链）
# ---------------------------------------------------------------------------

def apply_carryover(
    db: Session, carryover: models.CreditCarryover
) -> Optional[models.CreditBatch]:
    """结转落地：从来源批次转出消耗，生成结转新批次并保留来源链（幂等）。"""
    existing = db.query(models.CreditBatch).filter(
        models.CreditBatch.source_type == "credit_carryover",
        models.CreditBatch.source_id == carryover.id,
    ).first()
    if existing:
        return existing
    if not _has_any_batches(db, carryover.enterprise_id):
        return None  # 存量无台账数据，走旧逻辑

    amount = ROUND(carryover.carryover_amount)
    out_allocation = _select_and_apply(
        db,
        enterprise_id=carryover.enterprise_id,
        amount=amount,
        purpose=AllocationPurpose.CARRYOVER_OUT,
        purpose_key="carryover_out",
        year=carryover.to_year,
        reference_type="credit_carryover",
        reference_id=carryover.id,
        delta_mode="consume",
    )

    rule = get_active_rule(db)
    params = rule_params(rule)
    expiry_year = _validity_expiry(params, AcquisitionMethod.CARRYOVER.value, carryover.to_year)
    new_batch = _create_batch(
        db,
        enterprise_id=carryover.enterprise_id,
        method=AcquisitionMethod.CARRYOVER,
        source_year=carryover.from_year,
        acquired_year=carryover.to_year,
        expiry_year=expiry_year,
        source_type="credit_carryover",
        source_id=carryover.id,
    )
    in_allocation = _create_allocation(
        db,
        enterprise_id=carryover.enterprise_id,
        purpose=AllocationPurpose.CARRYOVER_IN,
        year=carryover.to_year,
        total_amount=amount,
        explanation=(
            f"{carryover.from_year}年度结转至{carryover.to_year}年度形成新批次"
            f"{new_batch.batch_no}，适用期限至{expiry_year}年末，来源链已保留"
        ),
        rule=rule,
        reference_type="credit_carryover",
        reference_id=carryover.id,
    )
    _add_item(db, in_allocation, new_batch, amount,
              f"结转转入，适用期限至{expiry_year}年末",
              d_initial=amount, d_remaining=amount)

    for item in out_allocation.items:
        db.add(models.CreditBatchLineage(
            child_batch_id=new_batch.id,
            parent_batch_id=item.batch_id,
            amount=item.amount,
        ))
    return new_batch


def get_batch_for_source(
    db: Session, source_type: str, source_id: int
) -> Optional[models.CreditBatch]:
    return db.query(models.CreditBatch).filter(
        models.CreditBatch.source_type == source_type,
        models.CreditBatch.source_id == source_id,
    ).first()


# ---------------------------------------------------------------------------
# 过期处理：只核销未预留部分，不动未完成交易的预留量
# ---------------------------------------------------------------------------

def expire_batches(
    db: Session, as_of_year: int, enterprise_id: Optional[int] = None
) -> List[models.BatchAllocation]:
    """过期核销：适用期限早于 as_of_year 的批次，其未预留剩余量核销。

    已预留给未完成交易（挂单）的数量保留不动，待成交核销或撤单释放。
    """
    query = db.query(models.CreditBatch).filter(
        models.CreditBatch.status == CreditBatchStatus.ACTIVE,
        models.CreditBatch.expiry_year < as_of_year,
        models.CreditBatch.remaining_amount > EPSILON,
    )
    if enterprise_id:
        query = query.filter(models.CreditBatch.enterprise_id == enterprise_id)
    batches = query.order_by(models.CreditBatch.enterprise_id, models.CreditBatch.id).all()

    by_enterprise: Dict[int, List[models.CreditBatch]] = {}
    for batch in batches:
        by_enterprise.setdefault(batch.enterprise_id, []).append(batch)

    allocations = []
    for ent_id, ent_batches in by_enterprise.items():
        total = ROUND(sum(b.remaining_amount for b in ent_batches))
        allocation = _create_allocation(
            db,
            enterprise_id=ent_id,
            purpose=AllocationPurpose.EXPIRE,
            year=as_of_year,
            total_amount=total,
            explanation=(
                f"{as_of_year}年度过期处理：{len(ent_batches)}个批次已过适用期限，"
                f"未预留余额合计{total}分核销；已预留给未完成交易的数量保留不动"
            ),
            reference_type="expire",
        )
        for batch in ent_batches:
            amount = ROUND(batch.remaining_amount)
            reserved_note = (
                f"；预留{ROUND(batch.reserved_amount)}分保留，待订单成交或撤单"
                if batch.reserved_amount > EPSILON else ""
            )
            _add_item(
                db, allocation, batch, amount,
                f"批次{batch.batch_no}适用期限至{batch.expiry_year}年末，"
                f"{as_of_year}年起不可用，未预留余额核销{reserved_note}",
                d_remaining=-amount,
                d_expired=amount,
            )
        allocations.append(allocation)
    return allocations


# ---------------------------------------------------------------------------
# 汇总视图 / 守恒校验 / 重放
# ---------------------------------------------------------------------------

def get_compliance_fulfilled_total(db: Session, enterprise_id: int, year: int) -> float:
    total = db.query(func.coalesce(func.sum(models.BatchAllocation.total_amount), 0.0)).filter(
        models.BatchAllocation.enterprise_id == enterprise_id,
        models.BatchAllocation.purpose == AllocationPurpose.COMPLIANCE,
        models.BatchAllocation.status == AllocationStatus.ACTIVE,
        models.BatchAllocation.year == year,
    ).scalar()
    return ROUND(total)


def get_reserved_total(db: Session, enterprise_id: int) -> float:
    total = db.query(func.coalesce(func.sum(models.CreditBatch.reserved_amount), 0.0)).filter(
        models.CreditBatch.enterprise_id == enterprise_id
    ).scalar()
    return ROUND(total)


def get_available_balance(db: Session, enterprise_id: int) -> Optional[float]:
    """批次台账口径的可用余额（剩余量合计）。无台账返回 None（存量数据不限制）。"""
    if not _has_any_batches(db, enterprise_id):
        return None
    total = db.query(func.coalesce(func.sum(models.CreditBatch.remaining_amount), 0.0)).filter(
        models.CreditBatch.enterprise_id == enterprise_id
    ).scalar()
    return ROUND(total)


def get_expired_total(db: Session, enterprise_id: int) -> float:
    total = db.query(func.coalesce(func.sum(models.CreditBatch.expired_amount), 0.0)).filter(
        models.CreditBatch.enterprise_id == enterprise_id
    ).scalar()
    return ROUND(total)


def get_batches(
    db: Session,
    enterprise_id: Optional[int] = None,
    status: Optional[CreditBatchStatus] = None,
    acquisition_method: Optional[AcquisitionMethod] = None,
    source_year: Optional[int] = None,
    skip: int = 0,
    limit: int = 200,
) -> List[models.CreditBatch]:
    query = db.query(models.CreditBatch)
    if enterprise_id:
        query = query.filter(models.CreditBatch.enterprise_id == enterprise_id)
    if status:
        query = query.filter(models.CreditBatch.status == status)
    if acquisition_method:
        query = query.filter(models.CreditBatch.acquisition_method == acquisition_method)
    if source_year:
        query = query.filter(models.CreditBatch.source_year == source_year)
    return query.order_by(models.CreditBatch.id).offset(skip).limit(limit).all()


def get_batch_balance(db: Session, enterprise_id: int) -> Dict:
    """余额拆分视图：把单一余额拆成可追踪的批次结构。"""
    batches = db.query(models.CreditBatch).filter(
        models.CreditBatch.enterprise_id == enterprise_id
    ).order_by(models.CreditBatch.id).all()

    by_method: Dict[str, Dict] = {}
    by_source_year: Dict[int, Dict] = {}
    expiring: Dict[int, float] = {}
    totals = {"initial": 0.0, "remaining": 0.0, "reserved": 0.0,
              "consumed": 0.0, "expired": 0.0}

    for batch in batches:
        totals["initial"] += batch.initial_amount
        totals["remaining"] += batch.remaining_amount
        totals["reserved"] += batch.reserved_amount
        totals["consumed"] += batch.consumed_amount
        totals["expired"] += batch.expired_amount

        method_key = batch.acquisition_method.value
        method_bucket = by_method.setdefault(method_key, {
            "acquisition_method": method_key,
            "method_label": METHOD_LABELS.get(method_key, method_key),
            "batch_count": 0, "remaining": 0.0, "reserved": 0.0,
            "consumed": 0.0, "expired": 0.0,
        })
        method_bucket["batch_count"] += 1
        method_bucket["remaining"] += batch.remaining_amount
        method_bucket["reserved"] += batch.reserved_amount
        method_bucket["consumed"] += batch.consumed_amount
        method_bucket["expired"] += batch.expired_amount

        year_bucket = by_source_year.setdefault(batch.source_year, {
            "source_year": batch.source_year,
            "batch_count": 0, "remaining": 0.0, "reserved": 0.0,
        })
        year_bucket["batch_count"] += 1
        year_bucket["remaining"] += batch.remaining_amount
        year_bucket["reserved"] += batch.reserved_amount

        if batch.status == CreditBatchStatus.ACTIVE and batch.remaining_amount > EPSILON:
            expiring[batch.expiry_year] = expiring.get(batch.expiry_year, 0.0) + batch.remaining_amount

    round_dict = lambda d: {k: (ROUND(v) if isinstance(v, float) else v) for k, v in d.items()}  # noqa: E731
    return {
        "enterprise_id": enterprise_id,
        "batch_count": len(batches),
        "total_initial": ROUND(totals["initial"]),
        "total_remaining": ROUND(totals["remaining"]),
        "total_reserved": ROUND(totals["reserved"]),
        "total_consumed": ROUND(totals["consumed"]),
        "total_expired": ROUND(totals["expired"]),
        "available_balance": ROUND(totals["remaining"]),
        "by_method": [round_dict(b) for b in by_method.values()],
        "by_source_year": [round_dict(by_source_year[y]) for y in sorted(by_source_year)],
        "expiring_by_year": [
            {"expiry_year": year, "remaining_at_risk": ROUND(amount)}
            for year, amount in sorted(expiring.items())
        ],
    }


def get_batch_lineage(db: Session, batch_id: int, depth: int = 0, max_depth: int = 10) -> Dict:
    """递归追溯批次来源链，直到最初核发批次。"""
    batch = db.get(models.CreditBatch, batch_id)
    if not batch:
        raise ValueError("批次不存在")
    node = {
        "batch_id": batch.id,
        "batch_no": batch.batch_no,
        "enterprise_id": batch.enterprise_id,
        "acquisition_method": batch.acquisition_method.value,
        "method_label": METHOD_LABELS.get(batch.acquisition_method.value, ""),
        "source_year": batch.source_year,
        "acquired_year": batch.acquired_year,
        "initial_amount": ROUND(batch.initial_amount),
        "remaining_amount": ROUND(batch.remaining_amount),
        "expiry_year": batch.expiry_year,
        "parents": [],
    }
    if depth >= max_depth:
        return node
    links = db.query(models.CreditBatchLineage).filter(
        models.CreditBatchLineage.child_batch_id == batch_id
    ).all()
    for link in links:
        parent_node = get_batch_lineage(db, link.parent_batch_id, depth + 1, max_depth)
        parent_node["transferred_amount"] = ROUND(link.amount)
        node["parents"].append(parent_node)
    return node


def check_conservation(db: Session, enterprise_id: Optional[int] = None) -> Dict:
    """数量守恒校验：

    1) 每个批次：初始量 == 剩余 + 预留 + 消耗 + 过期；
    2) 每个批次：台账 delta 累加重算 == 当前五个数量字段（台账即真相）；
    3) 各数量字段不为负。
    """
    query = db.query(models.CreditBatch)
    if enterprise_id:
        query = query.filter(models.CreditBatch.enterprise_id == enterprise_id)
    batches = query.order_by(models.CreditBatch.id).all()

    delta_rows = db.query(
        models.BatchAllocationItem.batch_id,
        func.coalesce(func.sum(models.BatchAllocationItem.delta_initial), 0.0),
        func.coalesce(func.sum(models.BatchAllocationItem.delta_remaining), 0.0),
        func.coalesce(func.sum(models.BatchAllocationItem.delta_reserved), 0.0),
        func.coalesce(func.sum(models.BatchAllocationItem.delta_consumed), 0.0),
        func.coalesce(func.sum(models.BatchAllocationItem.delta_expired), 0.0),
    ).group_by(models.BatchAllocationItem.batch_id).all()
    ledger = {row[0]: row[1:] for row in delta_rows}

    violations = []
    totals = {"initial": 0.0, "remaining": 0.0, "reserved": 0.0,
              "consumed": 0.0, "expired": 0.0}
    for batch in batches:
        totals["initial"] += batch.initial_amount
        totals["remaining"] += batch.remaining_amount
        totals["reserved"] += batch.reserved_amount
        totals["consumed"] += batch.consumed_amount
        totals["expired"] += batch.expired_amount

        parts = ROUND(batch.remaining_amount + batch.reserved_amount
                      + batch.consumed_amount + batch.expired_amount)
        if abs(parts - ROUND(batch.initial_amount)) > 0.02:
            violations.append(
                f"批次{batch.batch_no}：初始{ROUND(batch.initial_amount)} != 分项合计{parts}"
            )
        sums = ledger.get(batch.id)
        if sums:
            checks = [
                ("initial_amount", batch.initial_amount, sums[0]),
                ("remaining_amount", batch.remaining_amount, sums[1]),
                ("reserved_amount", batch.reserved_amount, sums[2]),
                ("consumed_amount", batch.consumed_amount, sums[3]),
                ("expired_amount", batch.expired_amount, sums[4]),
            ]
            for field_name, current, ledger_sum in checks:
                if abs(ROUND(current) - ROUND(ledger_sum)) > 0.02:
                    violations.append(
                        f"批次{batch.batch_no}：{field_name}当前{ROUND(current)} "
                        f"!= 台账重算{ROUND(ledger_sum)}"
                    )
        for field_name in ("initial_amount", "remaining_amount", "reserved_amount",
                           "consumed_amount", "expired_amount"):
            if getattr(batch, field_name) < -EPSILON:
                violations.append(
                    f"批次{batch.batch_no}：{field_name}={getattr(batch, field_name)} 为负"
                )

    return {
        "enterprise_id": enterprise_id,
        "is_conserved": len(violations) == 0,
        "batch_count": len(batches),
        "totals": {k: ROUND(v) for k, v in totals.items()},
        "violations": violations,
    }


def replay_allocation(
    db: Session, allocation_id: int, rule_version: Optional[str] = None
) -> Dict:
    """按原规则（或指定版本）重放一次历史分配，校验结果是否一致。

    选择是确定性的：候选快照 + 规则版本 ⇒ 唯一结果。
    """
    allocation = db.get(models.BatchAllocation, allocation_id)
    if not allocation:
        raise ValueError("分配记录不存在")
    if not allocation.candidates_snapshot:
        raise ValueError("该分配不含候选快照，无法重放（非规则选择类分配）")

    version = rule_version or allocation.rule_version
    rule = get_rule_by_version(db, version) if version else None
    if not rule:
        raise ValueError(f"规则版本 {version} 不存在，无法按该版本重放")
    params = rule_params(rule)
    snapshot = json.loads(allocation.candidates_snapshot)

    purpose_key_map = {
        AllocationPurpose.RESERVE: "reserve",
        AllocationPurpose.SALE: "sale",
        AllocationPurpose.COMPLIANCE: "compliance",
        AllocationPurpose.CARRYOVER_OUT: "carryover_out",
    }
    purpose_key = purpose_key_map.get(allocation.purpose, "compliance")

    expected: List[Tuple[int, float]] = []
    remaining = ROUND(allocation.total_amount)

    reserve_items = snapshot.get("reserve_items") or []
    if reserve_items:
        walked = walk_reservation_items(reserve_items, min(
            remaining, ROUND(sum(e.get("outstanding", 0.0) for e in reserve_items))))
        expected.extend((e["batch_id"], e["amount"]) for e in walked)
        remaining = ROUND(remaining - sum(e["amount"] for e in walked))

    candidate_dicts = snapshot.get("candidates") or []
    if remaining > EPSILON and candidate_dicts:
        candidates = [BatchCandidate(**c) for c in candidate_dicts]
        result = select_batches(
            candidates,
            remaining,
            strategy=rule.strategy,
            source_priority=params.get("source_priority"),
            eligible_methods=(params.get("eligible_sources") or {}).get(purpose_key),
            purpose=purpose_key,
            rule_version=rule.version,
        )
        expected.extend((item.batch_id, item.amount) for item in result.items)

    actual = [(item.batch_id, ROUND(item.amount)) for item in allocation.items]
    matches = expected == actual

    batch_nos = {
        b.id: b.batch_no
        for b in db.query(models.CreditBatch).filter(
            models.CreditBatch.id.in_([bid for bid, _ in expected + actual] or [0])
        ).all()
    }
    return {
        "allocation_id": allocation.id,
        "allocation_no": allocation.allocation_no,
        "original_rule_version": allocation.rule_version,
        "replay_rule_version": rule.version,
        "replay_strategy": rule.strategy,
        "matches": matches,
        "replayed_items": [
            {"batch_id": bid, "batch_no": batch_nos.get(bid, ""), "amount": amount}
            for bid, amount in expected
        ],
        "recorded_items": [
            {"batch_id": bid, "batch_no": batch_nos.get(bid, ""), "amount": amount}
            for bid, amount in actual
        ],
        "explanation": (
            f"按规则{rule.version}重放分配{allocation.allocation_no}："
            + ("重放结果与历史记录一致" if matches else "重放结果与历史记录不一致，请核查")
        ),
    }
