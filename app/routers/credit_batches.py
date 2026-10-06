"""积分批次台账接口。

提供：
- 批次列表/余额（区分当年核发、历年结转、市场购入、退回恢复）；
- 批次来源链追溯（结转链逐级到最初核发）；
- 一次消费/预留的选批解释、历史重放、按其他规则版本对比重放；
- 年度履约、交易退回、过期作业；
- 数量守恒审计。
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import crud, schemas, batch_service
from ..database import get_db
from ..models import BatchStatus, BatchAcquisitionMethod

router = APIRouter(prefix="/credit-batches", tags=["credit-batches"])


def _batch_dict(b) -> dict:
    return {
        "id": b.id,
        "batch_no": b.batch_no,
        "enterprise_id": b.enterprise_id,
        "source_year": b.source_year,
        "origin_year": b.origin_year,
        "acquisition_method": b.acquisition_method.value
        if hasattr(b.acquisition_method, "value") else b.acquisition_method,
        "original_amount": round(b.original_amount, 2),
        "remaining_amount": round(b.remaining_amount, 2),
        "frozen_amount": round(b.frozen_amount, 2),
        "consumed_amount": round(b.consumed_amount, 2),
        "expired_amount": round(b.expired_amount, 2),
        "valid_from": b.valid_from,
        "valid_until": b.valid_until,
        "status": b.status.value if hasattr(b.status, "value") else b.status,
        "rule_version": b.rule_version,
        "origin_ref_type": b.origin_ref_type,
        "origin_ref_id": b.origin_ref_id,
        "parent_batch_id": b.parent_batch_id,
        "remark": b.remark,
        "created_at": b.created_at,
    }


@router.get("", response_model=List[schemas.BatchResponse])
def list_batches(
    enterprise_id: Optional[int] = None,
    status: Optional[BatchStatus] = None,
    acquisition_method: Optional[BatchAcquisitionMethod] = None,
    source_year: Optional[int] = None,
    db: Session = Depends(get_db),
):
    batches = batch_service.get_batches(
        db, enterprise_id=enterprise_id, status=status,
        acquisition_method=acquisition_method, source_year=source_year,
    )
    return [_batch_dict(b) for b in batches]


@router.get("/balance/{enterprise_id}")
def get_balance(
    enterprise_id: int,
    as_of: Optional[datetime] = None,
    db: Session = Depends(get_db),
):
    return batch_service.get_balance_summary(db, enterprise_id, as_of=as_of)


@router.get("/{batch_id}/lineage")
def get_lineage(batch_id: int, db: Session = Depends(get_db)):
    try:
        return batch_service.get_batch_lineage(db, batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/preview-selection")
def preview_selection(
    req: schemas.SelectionPreviewRequest,
    db: Session = Depends(get_db),
):
    try:
        return crud.preview_batch_selection(db, req.enterprise_id, req.amount, req.rule_version)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/fulfillment")
def fulfillment(req: schemas.FulfillmentRequest, db: Session = Depends(get_db)):
    try:
        return crud.fulfill_annual_obligation(
            db, req.enterprise_id, req.year, req.amount, req.rule_version, req.remark
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/transactions/{transaction_id}/return")
def return_transaction(transaction_id: int, db: Session = Depends(get_db)):
    try:
        return crud.return_credit_transaction(db, transaction_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/expire")
def run_expiry(as_of: Optional[datetime] = None, db: Session = Depends(get_db)):
    expired = crud.expire_due_batches(db, as_of=as_of)
    return {
        "as_of": (as_of or datetime.utcnow()).isoformat(),
        "expired_batch_count": len(expired),
        "details": expired,
        "note": "仅过期可用部分；预留给未完成交易的冻结数量予以保留",
    }


@router.get("/explain/group/{group_id}")
def explain_group(group_id: int, db: Session = Depends(get_db)):
    try:
        return batch_service.explain_group(db, group_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/explain/transaction/{transaction_id}")
def explain_transaction(transaction_id: int, db: Session = Depends(get_db)):
    try:
        return batch_service.explain_transaction(db, transaction_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/replay/{group_id}")
def replay_group(
    group_id: int,
    persist: bool = Query(False, description="是否标记该组已被重放"),
    db: Session = Depends(get_db),
):
    try:
        return batch_service.replay_group(db, group_id, persist=persist)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/replay/{group_id}/with-rule")
def replay_with_rule(
    group_id: int,
    req: schemas.ReplayRuleRequest,
    db: Session = Depends(get_db),
):
    try:
        return batch_service.replay_with_rule(db, group_id, req.rule_version)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/rules/versions")
def rule_versions():
    from ..batch_rules import available_rule_versions, RULE_DESCRIPTIONS
    return {
        "versions": [
            {"version": v, "description": RULE_DESCRIPTIONS[v]}
            for v in available_rule_versions()
        ]
    }


@router.get("/audit/conservation")
def audit_conservation(enterprise_id: Optional[int] = None, db: Session = Depends(get_db)):
    return batch_service.audit_conservation(db, enterprise_id=enterprise_id)
