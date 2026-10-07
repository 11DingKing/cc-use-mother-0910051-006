from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, schemas, batch_service, models
from ..database import get_db
from ..models import AcquisitionMethod, CreditBatchStatus, AllocationPurpose, AllocationStatus

router = APIRouter(prefix="/credit-batches", tags=["credit-batches"])


def _rule_to_response(rule: models.AllocationRule) -> dict:
    return {
        "id": rule.id,
        "version": rule.version,
        "name": rule.name,
        "strategy": rule.strategy,
        "params": batch_service.rule_params(rule),
        "is_active": rule.is_active,
        "description": rule.description,
        "created_at": rule.created_at,
    }


def _allocation_to_response(allocation: models.BatchAllocation) -> dict:
    return {
        "id": allocation.id,
        "allocation_no": allocation.allocation_no,
        "enterprise_id": allocation.enterprise_id,
        "purpose": allocation.purpose,
        "year": allocation.year,
        "total_amount": allocation.total_amount,
        "rule_version": allocation.rule_version,
        "status": allocation.status,
        "reference_type": allocation.reference_type,
        "reference_id": allocation.reference_id,
        "reverses_allocation_id": allocation.reverses_allocation_id,
        "explanation": allocation.explanation,
        "created_at": allocation.created_at,
        "reversed_at": allocation.reversed_at,
        "items": [
            {
                "id": item.id,
                "batch_id": item.batch_id,
                "batch_no": item.batch.batch_no if item.batch else None,
                "amount": item.amount,
                "remaining_before": item.remaining_before,
                "remaining_after": item.remaining_after,
                "reason": item.reason,
            }
            for item in allocation.items
        ],
    }


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------

@router.get("/rules", response_model=List[schemas.AllocationRuleResponse])
def list_rules(db: Session = Depends(get_db)):
    """批次选择规则版本列表（规则不可变，换版即新增版本）。"""
    rules = batch_service.get_rules(db)
    db.commit()
    return [_rule_to_response(r) for r in rules]


@router.post("/rules", response_model=schemas.AllocationRuleResponse)
def create_rule(rule_create: schemas.AllocationRuleCreate, db: Session = Depends(get_db)):
    """新增规则版本；历史分配仍按各自规则版本重放。"""
    valid_strategies = {"expiry_first", "fifo", "source_priority"}
    if rule_create.strategy not in valid_strategies:
        raise HTTPException(status_code=400, detail=f"策略必须是 {sorted(valid_strategies)} 之一")
    try:
        rule = batch_service.create_rule_version(
            db,
            version=rule_create.version,
            name=rule_create.name,
            strategy=rule_create.strategy,
            params=rule_create.params,
            description=rule_create.description,
            activate=rule_create.activate,
        )
        db.commit()
        db.refresh(rule)
        return _rule_to_response(rule)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/rules/{rule_id}/activate", response_model=schemas.AllocationRuleResponse)
def activate_rule(rule_id: int, db: Session = Depends(get_db)):
    """启用指定规则版本（同时停用其他版本，只影响之后的分配）。"""
    try:
        rule = batch_service.activate_rule(db, rule_id)
        db.commit()
        db.refresh(rule)
        return _rule_to_response(rule)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# 余额拆分视图 / 守恒校验
# ---------------------------------------------------------------------------

@router.get("/balance/{enterprise_id}")
def get_balance(enterprise_id: int, db: Session = Depends(get_db)):
    """余额拆分视图：按取得方式、来源年度、适用期限拆分企业积分余额。"""
    enterprise = crud.get_enterprise(db, enterprise_id)
    if not enterprise:
        raise HTTPException(status_code=404, detail="企业不存在")
    return batch_service.get_batch_balance(db, enterprise_id)


@router.get("/conservation/{enterprise_id}")
def check_conservation(enterprise_id: int, db: Session = Depends(get_db)):
    """数量守恒校验：批次分项合计=初始量，且台账重算与当前状态一致。"""
    return batch_service.check_conservation(db, enterprise_id)


# ---------------------------------------------------------------------------
# 批次查询
# ---------------------------------------------------------------------------

@router.get("/batches", response_model=List[schemas.CreditBatchResponse])
def list_batches(
    enterprise_id: Optional[int] = None,
    status: Optional[CreditBatchStatus] = None,
    acquisition_method: Optional[AcquisitionMethod] = None,
    source_year: Optional[int] = None,
    skip: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    return batch_service.get_batches(
        db,
        enterprise_id=enterprise_id,
        status=status,
        acquisition_method=acquisition_method,
        source_year=source_year,
        skip=skip,
        limit=limit,
    )


@router.get("/batches/{batch_id}", response_model=schemas.CreditBatchResponse)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(models.CreditBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


@router.get("/batches/{batch_id}/lineage")
def get_batch_lineage(batch_id: int, db: Session = Depends(get_db)):
    """批次来源链：递归追溯至最初核发批次（结转/购入均保留来源链）。"""
    try:
        return batch_service.get_batch_lineage(db, batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# 分配台账：查询、解释、回退、重放
# ---------------------------------------------------------------------------

@router.get("/allocations", response_model=List[schemas.BatchAllocationResponse])
def list_allocations(
    enterprise_id: Optional[int] = None,
    purpose: Optional[AllocationPurpose] = None,
    year: Optional[int] = None,
    status: Optional[AllocationStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(models.BatchAllocation)
    if enterprise_id:
        query = query.filter(models.BatchAllocation.enterprise_id == enterprise_id)
    if purpose:
        query = query.filter(models.BatchAllocation.purpose == purpose)
    if year:
        query = query.filter(models.BatchAllocation.year == year)
    if status:
        query = query.filter(models.BatchAllocation.status == status)
    allocations = query.order_by(models.BatchAllocation.id.desc()).offset(skip).limit(limit).all()
    return [_allocation_to_response(a) for a in allocations]


@router.get("/allocations/{allocation_id}", response_model=schemas.BatchAllocationResponse)
def get_allocation(allocation_id: int, db: Session = Depends(get_db)):
    """分配详情：一次消费为何选择这些批次（规则版本 + 逐项原因）。"""
    allocation = db.get(models.BatchAllocation, allocation_id)
    if not allocation:
        raise HTTPException(status_code=404, detail="分配记录不存在")
    return _allocation_to_response(allocation)


@router.post("/allocations/{allocation_id}/reverse", response_model=List[schemas.BatchAllocationResponse])
def reverse_allocation(
    allocation_id: int,
    reverse_request: schemas.ReverseAllocationRequest,
    db: Session = Depends(get_db),
):
    """退回：按台账明细原路恢复（履约退回/出售退回），全程数量守恒。"""
    try:
        created = batch_service.reverse_allocation(db, allocation_id, reverse_request.reason)
        allocation = db.get(models.BatchAllocation, allocation_id)
        crud.update_annual_summary_with_transactions(db, allocation.enterprise_id, allocation.year)
        if allocation.purpose == AllocationPurpose.SALE and allocation.reference_id:
            txn = db.get(models.CreditTransaction, allocation.reference_id)
            if txn:
                crud.update_annual_summary_after_transaction(db, txn, allocation.year)
        db.commit()
        return [_allocation_to_response(a) for a in created]
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/allocations/{allocation_id}/replay")
def replay_allocation(
    allocation_id: int,
    replay_request: schemas.ReplayAllocationRequest,
    db: Session = Depends(get_db),
):
    """按原规则（或指定版本）重放历史分配：规则换版后历史结果仍可复算。"""
    try:
        return batch_service.replay_allocation(db, allocation_id, replay_request.rule_version)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# 履约 / 过期 / 交易退回 / 存量迁移
# ---------------------------------------------------------------------------

@router.post("/compliance/fulfill", response_model=schemas.BatchAllocationResponse)
def fulfill_compliance(
    fulfill_request: schemas.ComplianceFulfillRequest,
    db: Session = Depends(get_db),
):
    """履约：按当前规则选择批次抵偿年度缺口，返回选择依据。"""
    enterprise = crud.get_enterprise(db, fulfill_request.enterprise_id)
    if not enterprise:
        raise HTTPException(status_code=404, detail="企业不存在")

    summary = crud.update_annual_summary_with_transactions(
        db, fulfill_request.enterprise_id, fulfill_request.year
    )
    fulfilled = batch_service.get_compliance_fulfilled_total(
        db, fulfill_request.enterprise_id, fulfill_request.year
    )
    remaining_gap = round(max(0.0, summary.credit_gap - fulfilled), 2)
    if remaining_gap <= 0.01:
        raise HTTPException(status_code=400, detail=f"{fulfill_request.year}年度无履约缺口")

    amount = fulfill_request.amount if fulfill_request.amount is not None else remaining_gap
    amount = round(amount, 2)
    if amount > remaining_gap + 0.01:
        raise HTTPException(
            status_code=400,
            detail=f"履约数量{amount}分超过当前缺口{remaining_gap}分",
        )

    try:
        allocation = batch_service.fulfill_compliance(
            db, fulfill_request.enterprise_id, fulfill_request.year, amount
        )
        crud.update_annual_summary_with_transactions(
            db, fulfill_request.enterprise_id, fulfill_request.year
        )
        db.commit()
        db.refresh(allocation)
        return _allocation_to_response(allocation)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/expire")
def expire_batches(expire_request: schemas.ExpireBatchesRequest, db: Session = Depends(get_db)):
    """过期处理：核销已过适用期限批次的未预留余额，预留量保留不动。"""
    allocations = batch_service.expire_batches(
        db,
        as_of_year=expire_request.as_of_year,
        enterprise_id=expire_request.enterprise_id,
    )
    for allocation in allocations:
        crud.update_annual_summary_with_transactions(db, allocation.enterprise_id, expire_request.as_of_year)
    db.commit()
    return {
        "success": True,
        "as_of_year": expire_request.as_of_year,
        "allocation_count": len(allocations),
        "total_expired": round(sum(a.total_amount for a in allocations), 2),
        "details": [
            {
                "allocation_no": a.allocation_no,
                "enterprise_id": a.enterprise_id,
                "expired_amount": a.total_amount,
                "explanation": a.explanation,
            }
            for a in allocations
        ],
    }


@router.post("/transactions/{transaction_id}/return", response_model=List[schemas.BatchAllocationResponse])
def return_transaction(
    transaction_id: int,
    reverse_request: schemas.ReverseAllocationRequest,
    db: Session = Depends(get_db),
):
    """交易退回：卖方批次恢复、买方购入批次追回，交易标记为已退回。"""
    transaction = db.get(models.CreditTransaction, transaction_id)
    if not transaction:
        raise HTTPException(status_code=404, detail="交易不存在")
    try:
        created = batch_service.return_transaction(db, transaction_id, reverse_request.reason)
        year = transaction.transaction_date.year if transaction.transaction_date else datetime.now().year
        crud.update_annual_summary_after_transaction(db, transaction, year)
        db.commit()
        return [_allocation_to_response(a) for a in created]
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sync-issued")
def sync_issued_batches(db: Session = Depends(get_db)):
    """存量迁移：为已确认但未建批次的积分记录补建核发批次。"""
    created = batch_service.sync_issued_batches(db)
    db.commit()
    return {"success": True, "created_batches": created}
