from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from .. import crud, schemas
from ..database import get_db
from ..models import CarryoverStatus

router = APIRouter(prefix="/credit-carryover", tags=["credit-carryover"])


@router.post("/", response_model=schemas.CreditCarryover)
def create_carryover(
    carryover: schemas.CreditCarryoverCreate,
    db: Session = Depends(get_db)
):
    try:
        return crud.create_credit_carryover(db=db, carryover=carryover)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/", response_model=List[schemas.CreditCarryoverWithDetail])
def read_carryovers(
    enterprise_id: Optional[int] = None,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    status: Optional[CarryoverStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    return crud.get_credit_carryovers(
        db,
        enterprise_id=enterprise_id,
        from_year=from_year,
        to_year=to_year,
        status=status,
        skip=skip,
        limit=limit
    )


@router.get("/{carryover_id}", response_model=schemas.CreditCarryoverWithDetail)
def read_carryover(carryover_id: int, db: Session = Depends(get_db)):
    carryover = crud.get_credit_carryover(db, carryover_id=carryover_id)
    if not carryover:
        raise HTTPException(status_code=404, detail="结转记录不存在")
    return carryover


@router.put("/{carryover_id}", response_model=schemas.CreditCarryover)
def update_carryover(
    carryover_id: int,
    carryover_update: schemas.CreditCarryoverUpdate,
    db: Session = Depends(get_db)
):
    carryover = crud.get_credit_carryover(db, carryover_id=carryover_id)
    if not carryover:
        raise HTTPException(status_code=404, detail="结转记录不存在")
    
    update_data = carryover_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(carryover, key, value)
    
    if "status" in update_data and update_data["status"] == CarryoverStatus.APPROVED:
        carryover.approved_at = datetime.utcnow()
    
    db.commit()
    db.refresh(carryover)
    return carryover


@router.post("/execute/{from_year}/{to_year}")
def execute_yearly_carryover(
    from_year: int,
    to_year: int,
    db: Session = Depends(get_db)
):
    if to_year <= from_year:
        raise HTTPException(status_code=400, detail="目标年度必须大于来源年度")
    
    if to_year - from_year > 3:
        raise HTTPException(status_code=400, detail="最多只能结转3年")
    
    carryovers = crud.execute_yearly_carryover(db, from_year=from_year, to_year=to_year)
    
    return {
        "success": True,
        "message": f"成功执行{from_year}至{to_year}年度结转",
        "carryover_count": len(carryovers),
        "total_carryover_amount": round(sum(c.carryover_amount for c in carryovers), 2),
        "details": [
            {
                "enterprise_id": c.enterprise_id,
                "enterprise_name": c.enterprise.name if c.enterprise else "",
                "original_surplus": c.original_amount,
                "carryover_ratio": c.carryover_ratio,
                "carryover_amount": c.carryover_amount
            }
            for c in carryovers
        ]
    }


@router.get("/summary/{enterprise_id}/{year}", response_model=List[schemas.CarryoverSummaryResponse])
def get_carryover_summary(
    enterprise_id: int,
    year: int,
    db: Session = Depends(get_db)
):
    return crud.get_carryover_summary(db, enterprise_id=enterprise_id, year=year)


@router.get("/enterprise-multi-year/{enterprise_id}")
def get_enterprise_multi_year_summary(
    enterprise_id: int,
    start_year: int = Query(..., description="起始年度"),
    end_year: int = Query(..., description="结束年度"),
    db: Session = Depends(get_db)
):
    try:
        return crud.get_multi_year_summary(
            db,
            enterprise_id=enterprise_id,
            start_year=start_year,
            end_year=end_year
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/annual-summary/{enterprise_id}/{year}", response_model=schemas.AnnualCreditSummaryWithDetail)
def get_annual_summary(
    enterprise_id: int,
    year: int,
    db: Session = Depends(get_db)
):
    summary = crud.update_annual_summary_with_transactions(db, enterprise_id=enterprise_id, year=year)
    return summary


@router.get("/enterprise-credits-v2/{year}", response_model=List[dict])
def get_enterprise_credit_summaries_v2(
    year: int,
    db: Session = Depends(get_db)
):
    summaries = crud.calculate_all_enterprise_summaries_v2(db, year=year)
    return [
        {
            "enterprise_id": s.enterprise_id,
            "enterprise_name": s.enterprise_name,
            "total_positive_credit": s.total_positive_credit,
            "total_negative_credit": s.total_negative_credit,
            "net_credit": s.net_credit,
            "required_credit": s.required_credit,
            "credit_gap": s.credit_gap,
            "credit_surplus": s.credit_surplus,
            "compliance_rate": s.compliance_rate,
            "average_power_consumption": s.average_power_consumption,
            "weighted_power_consumption": s.weighted_power_consumption,
            "model_count": s.model_count,
            "compliant_model_count": s.compliant_model_count,
            "carryover_in": s.carryover_in,
            "carryover_out": s.carryover_out,
            "bought_credit": s.bought_credit,
            "sold_credit": s.sold_credit,
            "final_net_credit": s.final_net_credit,
            "final_credit_gap": s.final_credit_gap,
            "final_credit_surplus": s.final_credit_surplus,
            "is_compliant": s.is_compliant
        }
        for s in summaries
    ]


@router.get("/enterprise-v2/{enterprise_id}/{year}")
def get_single_enterprise_summary_v2(
    enterprise_id: int,
    year: int,
    db: Session = Depends(get_db)
):
    summary = crud.calculate_enterprise_credit_summary_v2(db, enterprise_id=enterprise_id, year=year)
    if not summary:
        raise HTTPException(status_code=404, detail="企业不存在")
    return {
        "enterprise_id": summary.enterprise_id,
        "enterprise_name": summary.enterprise_name,
        "total_positive_credit": summary.total_positive_credit,
        "total_negative_credit": summary.total_negative_credit,
        "net_credit": summary.net_credit,
        "required_credit": summary.required_credit,
        "credit_gap": summary.credit_gap,
        "credit_surplus": summary.credit_surplus,
        "compliance_rate": summary.compliance_rate,
        "average_power_consumption": summary.average_power_consumption,
        "weighted_power_consumption": summary.weighted_power_consumption,
        "model_count": summary.model_count,
        "compliant_model_count": summary.compliant_model_count,
        "carryover_in": summary.carryover_in,
        "carryover_out": summary.carryover_out,
        "bought_credit": summary.bought_credit,
        "sold_credit": summary.sold_credit,
        "final_net_credit": summary.final_net_credit,
        "final_credit_gap": summary.final_credit_gap,
        "final_credit_surplus": summary.final_credit_surplus,
        "is_compliant": summary.is_compliant
    }
