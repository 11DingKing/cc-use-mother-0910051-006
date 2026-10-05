from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from .. import crud, schemas
from ..database import get_db
from ..rules import calculate_power_consumption_limit, get_weight_suggestion

router = APIRouter(prefix="/vehicle-models", tags=["vehicle-models"])


@router.post("/", response_model=schemas.VehicleModel)
def create_vehicle_model(model: schemas.VehicleModelCreate, db: Session = Depends(get_db)):
    db_model = crud.get_vehicle_model_by_code(db, model_code=model.model_code)
    if db_model:
        raise HTTPException(status_code=400, detail="车型代码已存在")
    db_enterprise = crud.get_enterprise(db, enterprise_id=model.enterprise_id)
    if not db_enterprise:
        raise HTTPException(status_code=400, detail="所属企业不存在")
    return crud.create_vehicle_model(db=db, model=model)


@router.get("/", response_model=List[schemas.VehicleModelWithEnterprise])
def read_vehicle_models(
    enterprise_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    models = crud.get_vehicle_models(db, enterprise_id=enterprise_id, skip=skip, limit=limit)
    result = []
    for m in models:
        model_dict = schemas.VehicleModelWithEnterprise.model_validate(m).model_dump()
        model_dict["power_consumption_limit"] = calculate_power_consumption_limit(m.curb_weight)
        model_dict["unit_credit"] = (
            (model_dict["power_consumption_limit"] - m.power_consumption)
            / model_dict["power_consumption_limit"] * 2.5
        )
        result.append(model_dict)
    return result


@router.get("/{model_id}", response_model=schemas.VehicleModelWithEnterprise)
def read_vehicle_model(model_id: int, db: Session = Depends(get_db)):
    db_model = crud.get_vehicle_model(db, model_id=model_id)
    if db_model is None:
        raise HTTPException(status_code=404, detail="车型不存在")
    model_dict = schemas.VehicleModelWithEnterprise.model_validate(db_model).model_dump()
    model_dict["power_consumption_limit"] = calculate_power_consumption_limit(db_model.curb_weight)
    model_dict["unit_credit"] = (
        (model_dict["power_consumption_limit"] - db_model.power_consumption)
        / model_dict["power_consumption_limit"] * 2.5
    )
    return model_dict


@router.put("/{model_id}", response_model=schemas.VehicleModel)
def update_vehicle_model(
    model_id: int,
    model_update: schemas.VehicleModelUpdate,
    db: Session = Depends(get_db)
):
    db_model = crud.update_vehicle_model(db, model_id, model_update)
    if db_model is None:
        raise HTTPException(status_code=404, detail="车型不存在")
    return db_model


@router.delete("/{model_id}")
def delete_vehicle_model(model_id: int, db: Session = Depends(get_db)):
    success = crud.delete_vehicle_model(db, model_id)
    if not success:
        raise HTTPException(status_code=404, detail="车型不存在")
    return {"message": "删除成功"}


@router.get("/{model_id}/weight-suggestion", response_model=schemas.WeightSuggestionResponse)
def get_model_weight_suggestion(model_id: int, db: Session = Depends(get_db)):
    db_model = crud.get_vehicle_model(db, model_id=model_id)
    if db_model is None:
        raise HTTPException(status_code=404, detail="车型不存在")
    return get_weight_suggestion(db_model.curb_weight)
