"""积分批次台账服务。

所有会改变批次数量的操作都集中在本模块，并写入 CreditBatchLedgerEntry 流水。
单批次守恒不变式（误差 0.01 内）：

    original_amount = remaining_amount + frozen_amount
                      + consumed_amount + expired_amount

退回恢复到原批次时同时冲减 consumed_amount；退回到已过期父批次时生成带
parent_batch_id 的「退回恢复」新批次，原批次 consumed 不变。
结转按比例缩量是明示政策，缩量记录在 CreditBatchLink 中可逐段追溯。
"""
import json
from datetime import datetime
from typing import List, Optional, Tuple, Dict, Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .models import (
    CreditBatch, CreditBatchLink, CreditBatchSelectionGroup, CreditBatchAllocation,
    CreditBatchLedgerEntry, BatchAcquisitionMethod, BatchStatus,
    AllocationPurpose, AllocationStatus, CreditTransaction, CreditOrder, OrderStatus,
)
from .batch_rules import (
    BatchCandidate, SelectionResult, SelectionLine,
    select_batches, DEFAULT_RULE_VERSION, EPS,
)

CONSERVATION_TOLERANCE = 0.011
# 核发积分自来源年度起可结转的最大年数（与 rules.CARRYOVER_MAX_YEARS 对应）
ISSUE_VALID_YEARS = 3


def _now() -> datetime:
    return datetime.utcnow()


def _year_end(year: int) -> datetime:
    return datetime(year, 12, 31, 23, 59, 59)


def _round(x: float) -> float:
    return round(float(x or 0.0), 2)


# ---------------------------------------------------------------------------
# 编号
# ---------------------------------------------------------------------------

def generate_batch_no(db: Session, prefix: str = "BAT") -> str:
    ts = _now().strftime("%Y%m%d%H%M%S")
    count = db.query(func.count(CreditBatch.id)).scalar() or 0
    pending = sum(1 for o in db.new if isinstance(o, CreditBatch))
    return f"{prefix}{ts}{count + pending + 1:05d}"


def _generate_group_no(db: Session) -> str:
    ts = _now().strftime("%Y%m%d%H%M%S")
    count = db.query(func.count(CreditBatchSelectionGroup.id)).scalar() or 0
    pending = sum(1 for o in db.new if isinstance(o, CreditBatchSelectionGroup))
    return f"GRP{ts}{count + pending + 1:05d}"


# ---------------------------------------------------------------------------
# 流水与批次状态
# ---------------------------------------------------------------------------

def _ledger(
    db: Session, batch: CreditBatch, entry_type: str, amount: float,
    ref_type: Optional[str] = None, ref_id: Optional[int] = None,
    ref_no: Optional[str] = None, allocation_id: Optional[int] = None,
    remark: Optional[str] = None,
) -> CreditBatchLedgerEntry:
    entry = CreditBatchLedgerEntry(
        batch_id=batch.id,
        enterprise_id=batch.enterprise_id,
        entry_type=entry_type,
        amount=_round(amount),
        balance_after=_round(batch.remaining_amount),
        ref_type=ref_type,
        ref_id=ref_id,
        ref_no=ref_no,
        allocation_id=allocation_id,
        remark=remark,
    )
    db.add(entry)
    return entry


def _refresh_status(batch: CreditBatch) -> None:
    available_total = _round(batch.remaining_amount + batch.frozen_amount)
    if available_total <= EPS and batch.consumed_amount + batch.expired_amount > EPS:
        batch.status = BatchStatus.EXHAUSTED
    elif batch.remaining_amount <= EPS and batch.frozen_amount <= EPS and batch.expired_amount > EPS:
        batch.status = BatchStatus.EXPIRED
    else:
        batch.status = BatchStatus.ACTIVE


def issue_batch(
    db: Session,
    enterprise_id: int,
    source_year: int,
    amount: float,
    acquisition_method: BatchAcquisitionMethod,
    valid_from: Optional[datetime] = None,
    valid_until: Optional[datetime] = None,
    origin_year: Optional[int] = None,
    rule_version: str = DEFAULT_RULE_VERSION,
    origin_ref_type: Optional[str] = None,
    origin_ref_id: Optional[int] = None,
    parent_batch_id: Optional[int] = None,
    remark: Optional[str] = None,
    batch_prefix: str = "BAT",
) -> CreditBatch:
    """创建一批积分并登记入库流水。"""
    amount = _round(amount)
    if amount <= EPS:
        raise ValueError("入库数量必须大于0")

    valid_from = valid_from or datetime(source_year, 1, 1)
    if valid_until is None:
        valid_until = _year_end(source_year + ISSUE_VALID_YEARS)
    if valid_until <= valid_from:
        raise ValueError("适用期限止必须晚于期限起")

    batch = CreditBatch(
        batch_no=generate_batch_no(db, batch_prefix),
        enterprise_id=enterprise_id,
        source_year=source_year,
        origin_year=origin_year if origin_year is not None else source_year,
        acquisition_method=acquisition_method,
        original_amount=amount,
        remaining_amount=amount,
        frozen_amount=0.0,
        consumed_amount=0.0,
        expired_amount=0.0,
        valid_from=valid_from,
        valid_until=valid_until,
        status=BatchStatus.ACTIVE,
        rule_version=rule_version,
        origin_ref_type=origin_ref_type,
        origin_ref_id=origin_ref_id,
        parent_batch_id=parent_batch_id,
        remark=remark,
    )
    db.add(batch)
    db.flush()
    _ledger(db, batch, "issue", amount, origin_ref_type, origin_ref_id,
            remark=remark or f"批次入库：{acquisition_method.value}")
    db.flush()
    return batch


# ---------------------------------------------------------------------------
# 候选与选批组
# ---------------------------------------------------------------------------

def _to_candidate(batch: CreditBatch) -> BatchCandidate:
    return BatchCandidate(
        batch_id=batch.id,
        batch_no=batch.batch_no,
        source_year=batch.source_year,
        origin_year=batch.origin_year,
        acquisition_method=batch.acquisition_method.value
        if hasattr(batch.acquisition_method, "value") else batch.acquisition_method,
        remaining=_round(batch.remaining_amount),
        valid_from=batch.valid_from,
        valid_until=batch.valid_until,
        status=batch.status.value if hasattr(batch.status, "value") else batch.status,
        rule_version=batch.rule_version,
    )


def get_available_candidates(
    db: Session, enterprise_id: int, as_of: Optional[datetime] = None
) -> List[BatchCandidate]:
    """取企业当前可参与选批的批次：active 且有可用余额。

    只取 remaining_amount（可用部分）。frozen_amount 是预留给未完成交易的数量，
    天然不进入候选，过期作业与选批都不会触及它。
    """
    as_of = as_of or _now()
    batches = db.query(CreditBatch).filter(
        CreditBatch.enterprise_id == enterprise_id,
        CreditBatch.status == BatchStatus.ACTIVE,
        CreditBatch.remaining_amount > EPS,
    ).order_by(CreditBatch.id).all()
    return [_to_candidate(b) for b in batches if b.valid_until is None or b.valid_until >= as_of]


def get_available_balance(db: Session, enterprise_id: int, as_of: Optional[datetime] = None) -> float:
    return _round(sum(c.remaining for c in get_available_candidates(db, enterprise_id, as_of)))


def _persist_group(
    db: Session,
    enterprise_id: int,
    purpose: AllocationPurpose,
    amount: float,
    result: SelectionResult,
    candidates: List[BatchCandidate],
    as_of: datetime,
    ref_type: Optional[str] = None,
    ref_id: Optional[int] = None,
    ref_no: Optional[str] = None,
    replayed: bool = False,
) -> CreditBatchSelectionGroup:
    snapshot = {
        "as_of": as_of.isoformat(),
        "rule_version": result.rule_version,
        "requested_amount": _round(amount),
        "candidates": [c.to_snapshot() for c in candidates],
    }
    reason_summary = (
        f"{result.rule_description} "
        f"需求{_round(amount)}，命中{len(result.lines)}个批次，"
        f"共选{result.selected_amount}，缺口{result.shortfall}。"
    )
    group = CreditBatchSelectionGroup(
        group_no=_generate_group_no(db),
        enterprise_id=enterprise_id,
        purpose=purpose,
        amount=_round(amount),
        rule_version=result.rule_version,
        ref_type=ref_type,
        ref_id=ref_id,
        ref_no=ref_no,
        candidates_snapshot=json.dumps(snapshot, ensure_ascii=False),
        reason_summary=reason_summary,
        replayed=replayed,
    )
    db.add(group)
    db.flush()

    for line in result.lines:
        db.add(CreditBatchAllocation(
            group_id=group.id,
            batch_id=line.batch_id,
            enterprise_id=enterprise_id,
            purpose=purpose,
            amount=line.amount,
            status=AllocationStatus.HELD,
            reason=line.reason,
            order_id=None,
        ))
    db.flush()
    return group


def _run_selection(
    db: Session, enterprise_id: int, purpose: AllocationPurpose, amount: float,
    rule_version: str, as_of: datetime,
    ref_type: Optional[str] = None, ref_id: Optional[int] = None,
    ref_no: Optional[str] = None, candidates: Optional[List[BatchCandidate]] = None,
) -> Tuple[CreditBatchSelectionGroup, SelectionResult]:
    amount = _round(amount)
    if amount <= EPS:
        raise ValueError("消费数量必须大于0")
    if candidates is None:
        candidates = get_available_candidates(db, enterprise_id, as_of)
    result = select_batches(candidates, amount, as_of=as_of, rule_version=rule_version)
    if not result.is_satisfied:
        available = _round(sum(c.remaining for c in candidates))
        raise ValueError(
            f"可用积分不足：需求{amount}，当前可选{available}，缺口{result.shortfall}"
            f"（已冻结给未完成交易或已过期的数量不计入可用）"
        )
    group = _persist_group(
        db, enterprise_id, purpose, amount, result, candidates, as_of,
        ref_type=ref_type, ref_id=ref_id, ref_no=ref_no,
    )
    return group, result


# ---------------------------------------------------------------------------
# 预留（卖单挂出）/ 成交 / 撤单
# ---------------------------------------------------------------------------

def reserve_for_order(
    db: Session, order: CreditOrder, rule_version: str = DEFAULT_RULE_VERSION
) -> CreditBatchSelectionGroup:
    """卖单挂出时按规则挑选批次并冻结（remaining -> frozen）。"""
    as_of = _now()
    group, result = _run_selection(
        db, order.enterprise_id, AllocationPurpose.RESERVE, order.total_amount,
        rule_version, as_of, ref_type="order", ref_id=order.id, ref_no=order.order_no,
    )
    for line, alloc in zip(result.lines, group.allocations):
        batch = db.get(CreditBatch, line.batch_id)
        batch.remaining_amount = _round(batch.remaining_amount - line.amount)
        batch.frozen_amount = _round(batch.frozen_amount + line.amount)
        alloc.order_id = order.id
        _ledger(db, batch, "reserve", -line.amount, "order", order.id, order.order_no,
                alloc.id, f"卖单{order.order_no}预留冻结")
        _refresh_status(batch)
    db.flush()
    return group


def adjust_reservation(
    db: Session, order: CreditOrder, old_total: float, new_total: float,
    rule_version: str = DEFAULT_RULE_VERSION,
) -> Optional[CreditBatchSelectionGroup]:
    """卖单改量：增量补冻结（重新选批），减量释放差额。"""
    old_total = _round(old_total)
    new_total = _round(new_total)
    delta = _round(new_total - old_total)
    if abs(delta) <= EPS:
        return None
    if delta < 0:
        release_reservation(db, order, amount=-delta)
        return None
    # 增量：对新增数量再做一次预留选批
    as_of = _now()
    group, result = _run_selection(
        db, order.enterprise_id, AllocationPurpose.RESERVE, delta,
        rule_version, as_of, ref_type="order", ref_id=order.id,
        ref_no=f"{order.order_no}#改量",
    )
    for line, alloc in zip(result.lines, group.allocations):
        batch = db.get(CreditBatch, line.batch_id)
        batch.remaining_amount = _round(batch.remaining_amount - line.amount)
        batch.frozen_amount = _round(batch.frozen_amount + line.amount)
        alloc.order_id = order.id
        _ledger(db, batch, "reserve", -line.amount, "order", order.id,
                f"{order.order_no}#改量", alloc.id, f"卖单改量补冻结{line.amount}")
        _refresh_status(batch)
    db.flush()
    return group


def _order_held_allocations(db: Session, order_id: int) -> List[CreditBatchAllocation]:    return db.query(CreditBatchAllocation).filter(
        CreditBatchAllocation.order_id == order_id,
        CreditBatchAllocation.status == AllocationStatus.HELD,
    ).order_by(CreditBatchAllocation.id).all()


def consume_reservation(
    db: Session, order: CreditOrder, transaction: CreditTransaction, amount: float,
) -> List[CreditBatchAllocation]:
    """卖单成交：把该卖单冻结的数量转为消耗（frozen -> consumed）。

    按预留时的排序位次依次消耗，部分成交只动前面的行；返回本次成交行。
    """
    amount = _round(amount)
    held = _order_held_allocations(db, order.id)
    held_total = _round(sum(a.amount - a.consumed_amount for a in held))
    if amount > held_total + CONSERVATION_TOLERANCE:
        raise ValueError(f"卖单{order.order_no}冻结量{held_total}不足以成交{amount}")

    need = amount
    consumed_rows: List[CreditBatchAllocation] = []
    for alloc in held:
        if need <= EPS:
            break
        available_held = _round(alloc.amount - alloc.consumed_amount)
        take = _round(min(need, available_held))
        if take <= EPS:
            continue
        batch = db.get(CreditBatch, alloc.batch_id)
        batch.frozen_amount = _round(batch.frozen_amount - take)
        batch.consumed_amount = _round(batch.consumed_amount + take)

        consumed_row = CreditBatchAllocation(
            group_id=alloc.group_id,
            batch_id=alloc.batch_id,
            enterprise_id=order.enterprise_id,
            purpose=AllocationPurpose.SELL,
            amount=take,
            status=AllocationStatus.CONSUMED,
            reason=f"卖单{order.order_no}成交，沿用预留行：{alloc.reason}",
            consumed_amount=take,
            order_id=order.id,
            transaction_id=transaction.id,
            source_allocation_id=alloc.id,
        )
        db.add(consumed_row)
        db.flush()
        alloc.consumed_amount = _round(alloc.consumed_amount + take)
        if _round(alloc.amount - alloc.consumed_amount) <= EPS:
            alloc.status = AllocationStatus.CONSUMED
        _ledger(db, batch, "consume", 0.0, "transaction", transaction.id,
                transaction.transaction_no, consumed_row.id,
                f"卖单成交：冻结转消耗{take}")
        _refresh_status(batch)
        consumed_rows.append(consumed_row)
        need = _round(need - take)

    if need > CONSERVATION_TOLERANCE:
        raise ValueError("成交消耗过程中数量不平，已回滚")
    db.flush()
    return consumed_rows


def release_reservation(
    db: Session, order: CreditOrder, amount: Optional[float] = None
) -> List[CreditBatchAllocation]:
    """撤单：把卖单仍冻结的数量退回可用（frozen -> remaining）。"""
    held = _order_held_allocations(db, order.id)
    releasable = _round(sum(a.amount - a.consumed_amount for a in held))
    if amount is None:
        amount = releasable
    amount = _round(amount)
    if amount > releasable + CONSERVATION_TOLERANCE:
        raise ValueError(f"卖单{order.order_no}可释放冻结量仅{releasable}，无法释放{amount}")

    need = amount
    released_rows: List[CreditBatchAllocation] = []
    for alloc in held:
        if need <= EPS:
            break
        available_held = _round(alloc.amount - alloc.consumed_amount)
        take = _round(min(need, available_held))
        if take <= EPS:
            continue
        batch = db.get(CreditBatch, alloc.batch_id)
        batch.frozen_amount = _round(batch.frozen_amount - take)
        batch.remaining_amount = _round(batch.remaining_amount + take)
        alloc.released_amount = _round(alloc.released_amount + take)
        if _round(alloc.amount - alloc.consumed_amount - alloc.released_amount) <= EPS:
            alloc.status = AllocationStatus.RELEASED
        release_row = CreditBatchAllocation(
            group_id=alloc.group_id,
            batch_id=alloc.batch_id,
            enterprise_id=order.enterprise_id,
            purpose=AllocationPurpose.RESERVE,
            amount=take,
            status=AllocationStatus.RELEASED,
            reason=f"撤单{order.order_no}释放预留：{alloc.reason}",
            released_amount=take,
            order_id=order.id,
            source_allocation_id=alloc.id,
        )
        db.add(release_row)
        db.flush()
        _ledger(db, batch, "release", take, "order", order.id, order.order_no,
                release_row.id, f"撤单释放冻结{take}")
        _refresh_status(batch)
        released_rows.append(release_row)
        need = _round(need - take)

    if need > CONSERVATION_TOLERANCE:
        raise ValueError("撤单释放过程中数量不平，已回滚")
    db.flush()
    return released_rows


def consume_direct(
    db: Session, enterprise_id: int, purpose: AllocationPurpose, amount: float,
    rule_version: str = DEFAULT_RULE_VERSION,
    ref_type: Optional[str] = None, ref_id: Optional[int] = None,
    ref_no: Optional[str] = None,
) -> Tuple[CreditBatchSelectionGroup, List[CreditBatchAllocation]]:
    """无挂单的直接消耗（年度履约、协议出售）：选批后 remaining -> consumed。"""
    as_of = _now()
    group, result = _run_selection(
        db, enterprise_id, purpose, amount, rule_version, as_of,
        ref_type=ref_type, ref_id=ref_id, ref_no=ref_no,
    )
    rows = []
    for line, alloc in zip(result.lines, group.allocations):
        batch = db.get(CreditBatch, line.batch_id)
        batch.remaining_amount = _round(batch.remaining_amount - line.amount)
        batch.consumed_amount = _round(batch.consumed_amount + line.amount)
        alloc.status = AllocationStatus.CONSUMED
        alloc.consumed_amount = line.amount
        _ledger(db, batch, "consume", -line.amount, ref_type, ref_id, ref_no,
                alloc.id, f"{purpose.value}直接消耗")
        _refresh_status(batch)
        rows.append(alloc)
    db.flush()
    return group, rows


# ---------------------------------------------------------------------------
# 市场购入（成交时买方入库，继承卖方批次的有效期与最初核发年度）
# ---------------------------------------------------------------------------

def receive_purchase_lines(
    db: Session, buyer_enterprise_id: int, transaction: CreditTransaction,
    seller_lines: List[CreditBatchAllocation],
) -> List[CreditBatch]:
    """买方按卖方实际成交行逐批入库「市场购入」批次。

    购入批次继承卖方批次的 valid_until 与 origin_year，保证适用期限与来源链
    信息在转移后不失真；source_year 记交易发生年度。
    """
    txn_year = (transaction.transaction_date or _now()).year
    batches = []
    for line in seller_lines:
        seller_batch = db.get(CreditBatch, line.batch_id)
        batch = issue_batch(
            db,
            enterprise_id=buyer_enterprise_id,
            source_year=txn_year,
            amount=line.amount,
            acquisition_method=BatchAcquisitionMethod.MARKET_PURCHASE,
            valid_from=transaction.transaction_date or _now(),
            valid_until=seller_batch.valid_until,
            origin_year=seller_batch.origin_year,
            origin_ref_type="transaction",
            origin_ref_id=transaction.id,
            remark=f"市场购入：交易{transaction.transaction_no}，卖方批次{seller_batch.batch_no}",
            batch_prefix="BUY",
        )
        batches.append(batch)
    return batches


# ---------------------------------------------------------------------------
# 退回（交易回退）
# ---------------------------------------------------------------------------

def return_transaction(
    db: Session, transaction: CreditTransaction,
) -> Dict[str, Any]:
    """退回一笔已完成交易：

    - 卖方：逐笔成交消耗行恢复。原批次仍为 active/可接纳时恢复进原批次
      （remaining 回升、consumed 冲减）；原批次已耗尽/过期则生成带 parent 链的
      「退回恢复」批次，有效期按退回年度重新核发政策计算。
    - 买方：冲回其因本交易入库的市场购入批次；若该批次已被耗用或过期，
      在结果中如实报告无法冲回的数量（不做假恢复）。
    """
    if transaction.status != "completed":
        raise ValueError("只有已完成状态的交易可以退回")

    result: Dict[str, Any] = {
        "transaction_id": transaction.id,
        "transaction_no": transaction.transaction_no,
        "seller_restored": [],
        "buyer_reversed": [],
        "buyer_unrecoverable": 0.0,
    }

    # 1) 卖方恢复
    sell_rows = db.query(CreditBatchAllocation).filter(
        CreditBatchAllocation.transaction_id == transaction.id,
        CreditBatchAllocation.purpose == AllocationPurpose.SELL,
        CreditBatchAllocation.status == AllocationStatus.CONSUMED,
    ).order_by(CreditBatchAllocation.id).all()

    for row in sell_rows:
        amount = _round(row.amount - row.restored_amount)
        if amount <= EPS:
            continue
        parent = db.get(CreditBatch, row.batch_id)
        # 父批次仍在有效期内（即便已耗尽）即可恢复进原批次并复活；
        # 只有父批次已过期/缺失时才生成带链的退回恢复新批次
        parent_revivable = (
            parent is not None
            and parent.valid_until is not None and parent.valid_until >= _now()
        )
        if parent_revivable:
            parent.consumed_amount = _round(parent.consumed_amount - amount)
            parent.remaining_amount = _round(parent.remaining_amount + amount)
            restore_batch_id = parent.id
            target_no = parent.batch_no
            _refresh_status(parent)
            _ledger(db, parent, "restore", amount, "transaction", transaction.id,
                    transaction.transaction_no, row.id,
                    f"交易退回恢复进原批次{parent.batch_no}")
        else:
            cur_year = (transaction.transaction_date or _now()).year
            restore_batch = issue_batch(
                db,
                enterprise_id=transaction.from_enterprise_id,
                source_year=cur_year,
                amount=amount,
                acquisition_method=BatchAcquisitionMethod.RETURN,
                valid_from=_now(),
                valid_until=_year_end(cur_year + ISSUE_VALID_YEARS),
                origin_year=parent.origin_year if parent else cur_year,
                origin_ref_type="transaction_return",
                origin_ref_id=transaction.id,
                parent_batch_id=parent.id if parent else None,
                remark=f"交易退回：父批次{parent.batch_no if parent else 'N/A'}已耗尽/过期，"
                       f"按退回恢复政策生成新批次",
                batch_prefix="RET",
            )
            restore_batch_id = restore_batch.id
            target_no = restore_batch.batch_no

        row.restored_amount = _round(row.restored_amount + amount)
        if _round(row.amount - row.restored_amount) <= EPS:
            row.status = AllocationStatus.RESTORED
        result["seller_restored"].append({
            "from_batch_no": parent.batch_no if parent else None,
            "restore_batch_no": target_no,
            "amount": amount,
            "new_batch": not parent_revivable,
        })

    # 2) 买方冲回购入批次
    buy_batches = db.query(CreditBatch).filter(
        CreditBatch.enterprise_id == transaction.to_enterprise_id,
        CreditBatch.origin_ref_type == "transaction",
        CreditBatch.origin_ref_id == transaction.id,
    ).order_by(CreditBatch.id).all()

    unrecoverable = 0.0
    for batch in buy_batches:
        # 该批次可能已被部分消耗/过期；只能从未被占用的 remaining 中冲回
        removable = _round(batch.remaining_amount)
        original = _round(batch.original_amount)
        if removable <= EPS:
            unrecoverable = _round(unrecoverable + original)
            result["buyer_reversed"].append({
                "batch_no": batch.batch_no, "amount": 0.0,
                "unrecoverable": original,
            })
            continue
        removed = removable
        batch.remaining_amount = _round(batch.remaining_amount - removed)
        batch.consumed_amount = _round(batch.consumed_amount + removed)
        short = _round(original - removed)
        unrecoverable = _round(unrecoverable + short)
        _ledger(db, batch, "return_back", -removed, "transaction", transaction.id,
                transaction.transaction_no, remark=f"交易退回，买方冲回购入批次")
        _refresh_status(batch)
        result["buyer_reversed"].append({
            "batch_no": batch.batch_no, "amount": removed, "unrecoverable": short,
        })

    result["buyer_unrecoverable"] = unrecoverable
    transaction.status = "returned"
    db.flush()
    return result


# ---------------------------------------------------------------------------
# 结转（选批出库 + 子批次入库 + 来源链）
# ---------------------------------------------------------------------------

def carryover_enterprise_batches(
    db: Session, enterprise_id: int, from_year: int, to_year: int,
    ratio: float, carryover: models.CreditCarryover,
    rule_version: str = DEFAULT_RULE_VERSION,
    candidates: Optional[List[BatchCandidate]] = None,
    as_of: Optional[datetime] = None,
) -> Optional[CreditBatch]:
    """把企业 from_year 的可用批次按 ratio 结转为 to_year 的新批次。

    - 仅从来源年度为 from_year、且在业务时点 as_of 可用（未冻结、未过期）的批次中选批；
    - 父批次按「结转前」数量出库（remaining -> consumed，purpose=carryover）；
    - 子批次按 ratio 缩量入库（明示政策缩量），每个父批次一行 CreditBatchLink，
      保留来源链可逐级追溯到最初核发批次；
    - 无可结转数量时返回 None。
    """
    as_of = as_of or datetime(to_year, 1, 1)
    if candidates is None:
        all_candidates = get_available_candidates(db, enterprise_id, as_of)
        candidates = [c for c in all_candidates if c.source_year == from_year]
    pre_amount = _round(sum(c.remaining for c in candidates))
    if pre_amount <= EPS:
        return None
    post_amount = _round(pre_amount * ratio)
    if post_amount <= EPS:
        return None

    group, result = _run_selection(
        db, enterprise_id, AllocationPurpose.CARRYOVER, pre_amount,
        rule_version, as_of, ref_type="carryover", ref_id=carryover.id,
        ref_no=carryover.carryover_no, candidates=candidates,
    )

    # 父批次出库
    parent_amounts: List[Tuple[int, str, float]] = []
    for line, alloc in zip(result.lines, group.allocations):
        batch = db.get(CreditBatch, line.batch_id)
        batch.remaining_amount = _round(batch.remaining_amount - line.amount)
        batch.consumed_amount = _round(batch.consumed_amount + line.amount)
        alloc.status = AllocationStatus.CONSUMED
        alloc.consumed_amount = line.amount
        _ledger(db, batch, "carryover_out", -line.amount, "carryover",
                carryover.id, carryover.carryover_no, alloc.id,
                f"结转{from_year}->{to_year}出库")
        _refresh_status(batch)
        parent_amounts.append((batch.id, batch.batch_no, line.amount))

    # 子批次入库：有效期取父批次最早到期日与目标年度政策上限的较小值
    parent_batches = [db.get(CreditBatch, pid) for pid, _, _ in parent_amounts]
    inherited_until = min(
        (b.valid_until for b in parent_batches if b.valid_until is not None),
        default=_year_end(to_year + ISSUE_VALID_YEARS),
    )
    policy_until = _year_end(to_year + ISSUE_VALID_YEARS)
    child_valid_until = min(inherited_until, policy_until)

    child = issue_batch(
        db,
        enterprise_id=enterprise_id,
        source_year=to_year,
        amount=post_amount,
        acquisition_method=BatchAcquisitionMethod.CARRYOVER,
        valid_from=as_of,
        valid_until=child_valid_until,
        origin_year=min(b.origin_year for b in parent_batches),
        origin_ref_type="carryover",
        origin_ref_id=carryover.id,
        remark=f"{from_year}年度结转至{to_year}年度，比例{ratio}",
        batch_prefix="CAR",
    )
    _ledger(db, child, "carryover_in", post_amount, "carryover",
            carryover.id, carryover.carryover_no,
            remark=f"结转{from_year}->{to_year}入库，政策缩量后{post_amount}")

    for pid, pno, pamount in parent_amounts:
        child_share = _round(pamount * ratio)
        db.add(CreditBatchLink(
            parent_batch_id=pid,
            child_batch_id=child.id,
            parent_amount=pamount,
            child_amount=child_share,
            carryover_ratio=ratio,
            carryover_id=carryover.id,
        ))
    db.flush()
    return child


# ---------------------------------------------------------------------------
# 过期处理（不误伤冻结数量）
# ---------------------------------------------------------------------------

def expire_batches(db: Session, as_of: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """把有效期早于 as_of 的批次的「可用部分」转过期。

    frozen_amount 是预留给未完成交易的数量，保留不动；只过期 remaining。
    """
    as_of = as_of or _now()
    batches = db.query(CreditBatch).filter(
        CreditBatch.status == BatchStatus.ACTIVE,
        CreditBatch.valid_until < as_of,
        CreditBatch.remaining_amount > EPS,
    ).all()

    expired_info = []
    for batch in batches:
        amount = _round(batch.remaining_amount)
        batch.remaining_amount = 0.0
        batch.expired_amount = _round(batch.expired_amount + amount)
        _ledger(db, batch, "expire", -amount, remark=f"到期过期{amount}（冻结量保留）")
        _refresh_status(batch)
        expired_info.append({
            "batch_id": batch.id,
            "batch_no": batch.batch_no,
            "enterprise_id": batch.enterprise_id,
            "expired_amount": amount,
            "frozen_preserved": _round(batch.frozen_amount),
        })
    db.flush()
    return expired_info


# ---------------------------------------------------------------------------
# 查询：余额、来源链、解释、重放、审计
# ---------------------------------------------------------------------------

def get_batches(
    db: Session, enterprise_id: Optional[int] = None,
    status: Optional[BatchStatus] = None,
    acquisition_method: Optional[BatchAcquisitionMethod] = None,
    source_year: Optional[int] = None,
) -> List[CreditBatch]:
    q = db.query(CreditBatch)
    if enterprise_id:
        q = q.filter(CreditBatch.enterprise_id == enterprise_id)
    if status:
        q = q.filter(CreditBatch.status == status)
    if acquisition_method:
        q = q.filter(CreditBatch.acquisition_method == acquisition_method)
    if source_year:
        q = q.filter(CreditBatch.source_year == source_year)
    return q.order_by(CreditBatch.valid_until, CreditBatch.source_year, CreditBatch.id).all()


def get_balance_summary(db: Session, enterprise_id: int, as_of: Optional[datetime] = None) -> Dict[str, Any]:
    as_of = as_of or _now()
    batches = db.query(CreditBatch).filter(CreditBatch.enterprise_id == enterprise_id).all()
    by_method: Dict[str, float] = {}
    total_remaining = total_frozen = total_consumed = total_expired = 0.0
    expiring: List[Dict[str, Any]] = []
    for b in batches:
        method = b.acquisition_method.value if hasattr(b.acquisition_method, "value") else b.acquisition_method
        by_method[method] = _round(by_method.get(method, 0.0) + b.remaining_amount)
        total_remaining = _round(total_remaining + b.remaining_amount)
        total_frozen = _round(total_frozen + b.frozen_amount)
        total_consumed = _round(total_consumed + b.consumed_amount)
        total_expired = _round(total_expired + b.expired_amount)
        if b.remaining_amount > EPS and b.valid_until and b.valid_until < as_of:
            expiring.append({"batch_no": b.batch_no, "remaining": _round(b.remaining_amount),
                             "valid_until": b.valid_until.strftime("%Y-%m-%d")})
    return {
        "enterprise_id": enterprise_id,
        "as_of": as_of.isoformat(),
        "available": total_remaining,
        "frozen": total_frozen,
        "consumed": total_consumed,
        "expired": total_expired,
        "usable_total": _round(total_remaining),
        "by_method": by_method,
        "already_expired_pending_run": expiring,
    }


def get_batch_lineage(db: Session, batch_id: int) -> Dict[str, Any]:
    """返回批次的完整来源链：向上追溯到最初核发，向下展开结转派生批次。"""
    batch = db.get(CreditBatch, batch_id)
    if not batch:
        raise ValueError("批次不存在")

    def brief(b: CreditBatch, amount: Optional[float] = None, ratio: Optional[float] = None) -> Dict[str, Any]:
        return {
            "batch_id": b.id,
            "batch_no": b.batch_no,
            "source_year": b.source_year,
            "origin_year": b.origin_year,
            "acquisition_method": b.acquisition_method.value
            if hasattr(b.acquisition_method, "value") else b.acquisition_method,
            "original_amount": _round(b.original_amount),
            "remaining_amount": _round(b.remaining_amount),
            "frozen_amount": _round(b.frozen_amount),
            "valid_until": b.valid_until.strftime("%Y-%m-%d") if b.valid_until else None,
            "link_parent_amount": _round(amount) if amount is not None else None,
            "link_ratio": ratio,
        }

    # 向上：本批次作为「子」的链指向其父亲
    ancestors: List[Dict[str, Any]] = []
    frontier = [batch]
    seen = set()
    while frontier:
        cur = frontier.pop()
        for link in cur.links_as_child:
            if link.parent_batch_id in seen:
                continue
            seen.add(link.parent_batch_id)
            pb = link.parent_batch
            ancestors.append(brief(pb, link.parent_amount, link.carryover_ratio))
            frontier.append(pb)
    ancestors.reverse()

    # 向下：本批次作为「父」的链指向其派生子女
    descendants: List[Dict[str, Any]] = []
    frontier = [batch]
    seen = set()
    while frontier:
        cur = frontier.pop()
        for link in cur.links_as_parent:
            if link.child_batch_id in seen:
                continue
            seen.add(link.child_batch_id)
            cb = link.child_batch
            descendants.append(brief(cb, link.child_amount, link.carryover_ratio))
            frontier.append(cb)

    return {
        "batch": brief(batch),
        "ancestors": ancestors,
        "descendants": descendants,
        "parent_batch_id": batch.parent_batch_id,
    }


def explain_group(db: Session, group_id: int) -> Dict[str, Any]:
    group = db.get(CreditBatchSelectionGroup, group_id)
    if not group:
        raise ValueError("选批组不存在")
    allocations = db.query(CreditBatchAllocation).filter(
        CreditBatchAllocation.group_id == group_id,
        # 只取该组本身用途的原始选批行；预留组下的成交(SELL)行是衍生行，不计入
        CreditBatchAllocation.purpose == group.purpose,
        CreditBatchAllocation.source_allocation_id.is_(None),
        CreditBatchAllocation.status.in_([
            AllocationStatus.HELD, AllocationStatus.CONSUMED, AllocationStatus.RELEASED,
        ]),
    ).order_by(CreditBatchAllocation.id).all()
    lines = []
    for a in allocations:
        b = db.get(CreditBatch, a.batch_id)
        lines.append({
            "allocation_id": a.id,
            "batch_id": a.batch_id,
            "batch_no": b.batch_no if b else None,
            "amount": _round(a.amount),
            "status": a.status.value,
            "source_year": b.source_year if b else None,
            "origin_year": b.origin_year if b else None,
            "acquisition_method": (b.acquisition_method.value
                                   if b and hasattr(b.acquisition_method, "value") else None),
            "valid_until": b.valid_until.strftime("%Y-%m-%d") if b and b.valid_until else None,
            "reason": a.reason,
        })
    return {
        "group_no": group.group_no,
        "purpose": group.purpose.value,
        "amount": _round(group.amount),
        "rule_version": group.rule_version,
        "ref_type": group.ref_type,
        "ref_id": group.ref_id,
        "ref_no": group.ref_no,
        "reason_summary": group.reason_summary,
        "candidates_snapshot": json.loads(group.candidates_snapshot or "{}"),
        "lines": lines,
        "created_at": group.created_at.isoformat() if group.created_at else None,
    }


def explain_transaction(db: Session, transaction_id: int) -> Dict[str, Any]:
    """一次成交为何选择这些批次：卖方消耗行（含预留来源）与买方购入批次。"""
    txn = db.get(CreditTransaction, transaction_id)
    if not txn:
        raise ValueError("交易不存在")
    sell_rows = db.query(CreditBatchAllocation).filter(
        CreditBatchAllocation.transaction_id == transaction_id,
    ).order_by(CreditBatchAllocation.id).all()
    lines = []
    for a in sell_rows:
        b = db.get(CreditBatch, a.batch_id)
        source = db.get(CreditBatchAllocation, a.source_allocation_id) if a.source_allocation_id else None
        lines.append({
            "batch_no": b.batch_no if b else None,
            "amount": _round(a.amount),
            "source_year": b.source_year if b else None,
            "origin_year": b.origin_year if b else None,
            "acquisition_method": (b.acquisition_method.value
                                   if b and hasattr(b.acquisition_method, "value") else None),
            "valid_until": b.valid_until.strftime("%Y-%m-%d") if b and b.valid_until else None,
            "reserved_by_group": source.group_id if source else None,
            "reason": a.reason,
        })
    buy_batches = db.query(CreditBatch).filter(
        CreditBatch.origin_ref_type == "transaction",
        CreditBatch.origin_ref_id == transaction_id,
    ).all()
    rule_versions = sorted({a.group.rule_version for a in sell_rows})
    return {
        "transaction_id": txn.id,
        "transaction_no": txn.transaction_no,
        "status": txn.status,
        "rule_versions": rule_versions,
        "seller_enterprise_id": txn.from_enterprise_id,
        "selected_lines": lines,
        "buyer_received_batches": [
            {
                "batch_no": b.batch_no,
                "amount": _round(b.original_amount),
                "source_year": b.source_year,
                "origin_year": b.origin_year,
                "valid_until": b.valid_until.strftime("%Y-%m-%d") if b.valid_until else None,
            } for b in buy_batches
        ],
    }


def replay_group(db: Session, group_id: int, persist: bool = False) -> Dict[str, Any]:
    """用选批组保存时的候选快照与规则版本重放，比较当时结果是否可复现。"""
    group = db.get(CreditBatchSelectionGroup, group_id)
    if not group:
        raise ValueError("选批组不存在")
    snapshot = json.loads(group.candidates_snapshot or "{}")
    as_of = datetime.fromisoformat(snapshot["as_of"])
    candidates = [BatchCandidate.from_snapshot(c) for c in snapshot.get("candidates", [])]
    replayed = select_batches(
        candidates, group.amount, as_of=as_of, rule_version=group.rule_version,
    )

    original_lines = db.query(CreditBatchAllocation).filter(
        CreditBatchAllocation.group_id == group_id,
        CreditBatchAllocation.purpose == group.purpose,
        CreditBatchAllocation.source_allocation_id.is_(None),
        CreditBatchAllocation.status.in_([
            AllocationStatus.HELD, AllocationStatus.CONSUMED, AllocationStatus.RELEASED,
        ]),
    ).order_by(CreditBatchAllocation.id).all()
    expected = [(a.batch_id, _round(a.amount)) for a in original_lines]
    actual = [(l.batch_id, _round(l.amount)) for l in replayed.lines]

    matches = expected == actual
    response = {
        "group_no": group.group_no,
        "rule_version": group.rule_version,
        "as_of": as_of.isoformat(),
        "matches_historical_result": matches,
        "historical_lines": [{"batch_id": bid, "amount": amt} for bid, amt in expected],
        "replayed_lines": [{"batch_id": bid, "amount": amt} for bid, amt in actual],
        "replayed_ranking": [
            {"rank": r.rank, "batch_no": r.batch_no, "chosen": r.chosen, "reason": r.reason}
            for r in replayed.ranking
        ],
        "excluded": replayed.excluded,
        "rule_description": replayed.rule_description,
    }
    if persist and not matches:
        group.replayed = True
        db.flush()
    return response


def replay_with_rule(
    db: Session, group_id: int, rule_version: str
) -> Dict[str, Any]:
    """用指定（可能是新版的）规则版本对同一份历史候选快照重新选批，对比差异。"""
    group = db.get(CreditBatchSelectionGroup, group_id)
    if not group:
        raise ValueError("选批组不存在")
    snapshot = json.loads(group.candidates_snapshot or "{}")
    as_of = datetime.fromisoformat(snapshot["as_of"])
    candidates = [BatchCandidate.from_snapshot(c) for c in snapshot.get("candidates", [])]
    alt = select_batches(candidates, group.amount, as_of=as_of, rule_version=rule_version)
    return {
        "group_no": group.group_no,
        "original_rule_version": group.rule_version,
        "requested_rule_version": rule_version,
        "as_of": as_of.isoformat(),
        "lines": [
            {
                "batch_id": l.batch_id, "batch_no": l.batch_no, "amount": l.amount,
                "rank": l.rank, "reason": l.reason,
            } for l in alt.lines
        ],
        "shortfall": alt.shortfall,
        "rule_description": alt.rule_description,
    }


def audit_conservation(db: Session, enterprise_id: Optional[int] = None) -> Dict[str, Any]:
    """核对每个批次的数量守恒，以及冻结量与挂单预留行之和一致。"""
    q = db.query(CreditBatch)
    if enterprise_id:
        q = q.filter(CreditBatch.enterprise_id == enterprise_id)
    batches = q.all()

    violations = []
    for b in batches:
        # 不变式：original = remaining + frozen + consumed + expired
        # （恢复进原批次时 consumed 已同步冲减，退回新批次另立批次记账）
        rhs = _round(b.remaining_amount + b.frozen_amount
                     + b.consumed_amount + b.expired_amount)
        delta = _round(b.original_amount - rhs)
        if abs(delta) > CONSERVATION_TOLERANCE:
            violations.append({
                "batch_no": b.batch_no,
                "original": _round(b.original_amount),
                "remaining": _round(b.remaining_amount),
                "frozen": _round(b.frozen_amount),
                "consumed": _round(b.consumed_amount),
                "expired": _round(b.expired_amount),
                "delta": delta,
            })

    # 冻结量 = 该批次所有未成交未释放预留行的净持有量
    frozen_violations = []
    for b in batches:
        held_rows = db.query(CreditBatchAllocation).filter(
            CreditBatchAllocation.batch_id == b.id,
            CreditBatchAllocation.purpose == AllocationPurpose.RESERVE,
            CreditBatchAllocation.status == AllocationStatus.HELD,
        ).all()
        held_sum = _round(sum(_round(a.amount - a.consumed_amount - a.released_amount)
                              for a in held_rows))
        if abs(held_sum - _round(b.frozen_amount)) > CONSERVATION_TOLERANCE:
            frozen_violations.append({
                "batch_no": b.batch_no,
                "frozen_amount": _round(b.frozen_amount),
                "held_rows_sum": held_sum,
            })

    # 全局双边核对：所有成交行卖方消耗之和 = 买方购入批次原始量之和
    txn_ids = [r[0] for r in db.query(CreditBatchAllocation.transaction_id).filter(
        CreditBatchAllocation.purpose == AllocationPurpose.SELL,
        CreditBatchAllocation.status.in_([AllocationStatus.CONSUMED, AllocationStatus.RESTORED]),
    ).distinct().all()]
    txn_violations = []
    for tid in txn_ids:
        if tid is None:
            continue
        sold = _round(sum(a.amount for a in db.query(CreditBatchAllocation).filter(
            CreditBatchAllocation.transaction_id == tid,
            CreditBatchAllocation.purpose == AllocationPurpose.SELL,
        ).all()))
        bought = _round(sum(x.original_amount for x in db.query(CreditBatch).filter(
            CreditBatch.origin_ref_type == "transaction",
            CreditBatch.origin_ref_id == tid,
        ).all()))
        if abs(sold - bought) > CONSERVATION_TOLERANCE:
            txn_violations.append({"transaction_id": tid, "sold": sold, "bought": bought})

    return {
        "checked_batches": len(batches),
        "conservation_ok": not violations,
        "freeze_ok": not frozen_violations,
        "transaction_balance_ok": not txn_violations,
        "batch_violations": violations,
        "freeze_violations": frozen_violations,
        "transaction_violations": txn_violations,
    }
