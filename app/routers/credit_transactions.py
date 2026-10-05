from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from .. import crud, schemas
from ..database import get_db

router = APIRouter(prefix="/credit-transactions", tags=["credit-transactions"])


@router.post("/", response_model=schemas.CreditTransaction)
def create_credit_transaction(
    transaction: schemas.CreditTransactionCreate,
    db: Session = Depends(get_db)
):
    if transaction.from_enterprise_id == transaction.to_enterprise_id:
        raise HTTPException(status_code=400, detail="交易双方不能是同一企业")

    from_ent = crud.get_enterprise(db, enterprise_id=transaction.from_enterprise_id)
    if not from_ent:
        raise HTTPException(status_code=400, detail="转出企业不存在")

    to_ent = crud.get_enterprise(db, enterprise_id=transaction.to_enterprise_id)
    if not to_ent:
        raise HTTPException(status_code=400, detail="转入企业不存在")

    if transaction.credit_amount <= 0:
        raise HTTPException(status_code=400, detail="交易积分数量必须大于0")

    db_txn = crud.create_credit_transaction(db=db, transaction=transaction)

    current_year = datetime.now().year
    crud.update_annual_summary_after_transaction(db, db_txn, current_year)
    db.refresh(db_txn)

    return db_txn


@router.get("/", response_model=List[schemas.CreditTransactionWithDetail])
def read_credit_transactions(
    enterprise_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    transactions = crud.get_credit_transactions(
        db, enterprise_id=enterprise_id, skip=skip, limit=limit
    )
    return transactions


@router.get("/{transaction_id}", response_model=schemas.CreditTransactionWithDetail)
def read_credit_transaction(transaction_id: int, db: Session = Depends(get_db)):
    transactions = crud.get_credit_transactions(db, limit=1000)
    transaction = None
    for t in transactions:
        if t.id == transaction_id:
            transaction = t
            break
    if not transaction:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    return transaction


@router.post("/match/{year}", response_model=schemas.MatchAndExecuteResponse)
def match_and_execute(
    year: int,
    unit_price: float = 3000.0,
    db: Session = Depends(get_db)
):
    transactions, remaining_gap, remaining_surplus = crud.match_and_execute_transactions(
        db, year=year, unit_price=unit_price
    )
    
    if not transactions:
        return schemas.MatchAndExecuteResponse(
            success=True,
            message="当前没有需要撮合的积分交易，所有企业积分均达标",
            transactions=[],
            remaining_gap=remaining_gap,
            remaining_surplus=remaining_surplus
        )
    
    return schemas.MatchAndExecuteResponse(
        success=True,
        message=f"成功完成 {len(transactions)} 笔积分撮合交易",
        transactions=transactions,
        remaining_gap=remaining_gap,
        remaining_surplus=remaining_surplus
    )


@router.get("/match-preview/{year}", response_model=List[schemas.MatchResultResponse])
def preview_match_transactions(
    year: int,
    unit_price: float = 3000.0,
    db: Session = Depends(get_db)
):
    summaries = crud.calculate_all_enterprise_summaries(db, year=year)
    from ..rules import match_credit_transactions
    results = match_credit_transactions(summaries, unit_price)
    
    return [
        schemas.MatchResultResponse(
            from_enterprise_id=r.from_enterprise_id,
            from_enterprise_name=r.from_enterprise_name,
            to_enterprise_id=r.to_enterprise_id,
            to_enterprise_name=r.to_enterprise_name,
            credit_amount=r.credit_amount,
            unit_price=r.unit_price,
            total_amount=r.total_amount
        )
        for r in results
    ]
