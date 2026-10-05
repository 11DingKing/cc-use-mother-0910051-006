from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from .. import crud, schemas
from ..database import get_db

router = APIRouter(prefix="/enterprises", tags=["enterprises"])


@router.post("/", response_model=schemas.Enterprise)
def create_enterprise(enterprise: schemas.EnterpriseCreate, db: Session = Depends(get_db)):
    db_enterprise = crud.get_enterprise_by_name(db, name=enterprise.name)
    if db_enterprise:
        raise HTTPException(status_code=400, detail="企业名称已存在")
    return crud.create_enterprise(db=db, enterprise=enterprise)


@router.get("/", response_model=List[schemas.Enterprise])
def read_enterprises(
    skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    enterprises = crud.get_enterprises(db, skip=skip, limit=limit)
    return enterprises


@router.get("/{enterprise_id}", response_model=schemas.Enterprise)
def read_enterprise(enterprise_id: int, db: Session = Depends(get_db)):
    db_enterprise = crud.get_enterprise(db, enterprise_id=enterprise_id)
    if db_enterprise is None:
        raise HTTPException(status_code=404, detail="企业不存在")
    return db_enterprise


@router.put("/{enterprise_id}", response_model=schemas.Enterprise)
def update_enterprise(
    enterprise_id: int,
    enterprise: schemas.EnterpriseUpdate,
    db: Session = Depends(get_db)
):
    db_enterprise = crud.update_enterprise(db, enterprise_id, enterprise)
    if db_enterprise is None:
        raise HTTPException(status_code=404, detail="企业不存在")
    return db_enterprise


@router.delete("/{enterprise_id}")
def delete_enterprise(enterprise_id: int, db: Session = Depends(get_db)):
    success = crud.delete_enterprise(db, enterprise_id)
    if not success:
        raise HTTPException(status_code=404, detail="企业不存在")
    return {"message": "删除成功"}
