from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from .. import crud, schemas
from ..database import get_db
from ..models import CreditRecordStatus

router = APIRouter(prefix="/credit-records", tags=["credit-records"])


@router.post("/calculate/{year}", response_model=List[schemas.CalculationResult])
def calculate_all_credits(year: int, db: Session = Depends(get_db)):
    records = crud.batch_calculate_credits(db, year=year)
    results = []
    for record in records:
        model = crud.get_vehicle_model(db, model_id=record.vehicle_model_id)
        results.append(schemas.CalculationResult(
            vehicle_model_id=record.vehicle_model_id,
            model_name=model.model_name if model else "未知车型",
            curb_weight=model.curb_weight if model else 0,
            power_consumption_limit=record.power_consumption_limit,
            actual_power_consumption=record.actual_power_consumption,
            unit_credit=record.unit_credit,
            annual_output=record.annual_output,
            total_credit=record.total_credit,
            is_compliant=record.actual_power_consumption <= record.power_consumption_limit
        ))
    return results


@router.post("/calculate/model/{model_id}", response_model=schemas.CalculationResult)
def calculate_single_model_credit(
    model_id: int,
    year: Optional[int] = None,
    db: Session = Depends(get_db)
):
    result = crud.calculate_vehicle_credit(db, model_id=model_id, year=year)
    if not result:
        raise HTTPException(status_code=404, detail="车型不存在")
    crud.create_credit_record(db, model_id=model_id, year=year or 2025)
    return result


@router.get("/", response_model=List[schemas.CreditRecordWithDetail])
def read_credit_records(
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[CreditRecordStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    records = crud.get_credit_records(
        db, enterprise_id=enterprise_id, year=year, status=status, skip=skip, limit=limit
    )
    result = []
    for r in records:
        record_dict = schemas.CreditRecordWithDetail.model_validate(r).model_dump()
        record_dict["enterprise"] = schemas.Enterprise.model_validate(r.vehicle_model.enterprise).model_dump()
        result.append(record_dict)
    return result


@router.get("/{record_id}", response_model=schemas.CreditRecordWithDetail)
def read_credit_record(record_id: int, db: Session = Depends(get_db)):
    records = crud.get_credit_records(db, limit=1)
    record = None
    for r in crud.get_credit_records(db, limit=1000):
        if r.id == record_id:
            record = r
            break
    if not record:
        raise HTTPException(status_code=404, detail="积分记录不存在")
    record_dict = schemas.CreditRecordWithDetail.model_validate(record).model_dump()
    record_dict["enterprise"] = schemas.Enterprise.model_validate(record.vehicle_model.enterprise).model_dump()
    return record_dict


@router.patch("/{record_id}/status", response_model=schemas.CreditRecord)
def update_credit_record_status(
    record_id: int,
    status_update: schemas.CreditRecordStatusUpdate,
    db: Session = Depends(get_db)
):
    try:
        record = crud.update_credit_record_status(db, record_id=record_id, status=status_update.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not record:
        raise HTTPException(status_code=404, detail="积分记录不存在")
    return record


@router.patch("/{record_id}", response_model=schemas.CreditRecord)
def update_credit_record(
    record_id: int,
    record_update: schemas.CreditRecordUpdate,
    db: Session = Depends(get_db)
):
    try:
        record = crud.update_credit_record(db, record_id=record_id, record_update=record_update)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not record:
        raise HTTPException(status_code=404, detail="积分记录不存在")
    return record


@router.patch("/year/{year}/status")
def batch_update_credit_records_status_by_year(
    year: int,
    status_update: schemas.CreditRecordStatusUpdate,
    db: Session = Depends(get_db)
):
    count = crud.batch_update_credit_records_status(db, year=year, status=status_update.status)
    return {
        "message": f"成功更新 {count} 条记录状态为 {status_update.status}",
        "count": count
    }
