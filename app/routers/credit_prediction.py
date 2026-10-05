from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from .. import crud, schemas
from ..database import get_db

router = APIRouter(prefix="/credit-prediction", tags=["credit-prediction"])


@router.post("/enterprise/{enterprise_id}", response_model=schemas.CreditPredictionResponse)
def predict_enterprise_credit(
    enterprise_id: int,
    prediction_request: schemas.CreditPredictionRequest,
    db: Session = Depends(get_db)
):
    try:
        return crud.predict_enterprise_credit(
            db,
            enterprise_id=enterprise_id,
            target_year=prediction_request.target_year,
            output_growth_rate=prediction_request.output_growth_rate,
            pc_improvement_rate=prediction_request.pc_improvement_rate
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/enterprise/{enterprise_id}/{target_year}", response_model=schemas.CreditPredictionResponse)
def get_enterprise_prediction(
    enterprise_id: int,
    target_year: int,
    output_growth_rate: float = Query(0.05, description="产量年增长率", ge=0, le=1),
    pc_improvement_rate: float = Query(0.02, description="电耗年改善率", ge=0, le=1),
    db: Session = Depends(get_db)
):
    try:
        return crud.predict_enterprise_credit(
            db,
            enterprise_id=enterprise_id,
            target_year=target_year,
            output_growth_rate=output_growth_rate,
            pc_improvement_rate=pc_improvement_rate
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/all/{target_year}", response_model=List[schemas.CreditPredictionResponse])
def predict_all_enterprises(
    target_year: int,
    output_growth_rate: float = Query(0.05, description="产量年增长率", ge=0, le=1),
    pc_improvement_rate: float = Query(0.02, description="电耗年改善率", ge=0, le=1),
    db: Session = Depends(get_db)
):
    return crud.predict_all_enterprises_credit(
        db,
        target_year=target_year,
        output_growth_rate=output_growth_rate,
        pc_improvement_rate=pc_improvement_rate
    )


@router.get("/summary/{target_year}")
def get_prediction_summary(
    target_year: int,
    output_growth_rate: float = Query(0.05, description="产量年增长率", ge=0, le=1),
    pc_improvement_rate: float = Query(0.02, description="电耗年改善率", ge=0, le=1),
    db: Session = Depends(get_db)
):
    predictions = crud.predict_all_enterprises_credit(
        db,
        target_year=target_year,
        output_growth_rate=output_growth_rate,
        pc_improvement_rate=pc_improvement_rate
    )

    total_positive = sum(p.predicted_total_positive for p in predictions)
    total_negative = sum(p.predicted_total_negative for p in predictions)
    total_net = sum(p.predicted_net_credit for p in predictions)

    compliant_count = sum(1 for p in predictions if p.predicted_net_credit >= 0)
    deficit_count = sum(1 for p in predictions if p.predicted_net_credit < 0)
    total_gap = sum(abs(p.predicted_net_credit) for p in predictions if p.predicted_net_credit < 0)
    total_surplus = sum(p.predicted_net_credit for p in predictions if p.predicted_net_credit > 0)

    avg_compliance_rate = sum(p.predicted_compliance_rate for p in predictions) / len(predictions) if predictions else 0

    return {
        "target_year": target_year,
        "assumptions": {
            "output_growth_rate": output_growth_rate,
            "pc_improvement_rate": pc_improvement_rate
        },
        "total_enterprises": len(predictions),
        "compliant_enterprises": compliant_count,
        "deficit_enterprises": deficit_count,
        "total_predicted_positive": round(total_positive, 2),
        "total_predicted_negative": round(total_negative, 2),
        "total_predicted_net": round(total_net, 2),
        "total_predicted_gap": round(total_gap, 2),
        "total_predicted_surplus": round(total_surplus, 2),
        "average_compliance_rate": round(avg_compliance_rate, 2),
        "enterprise_predictions": [
            {
                "enterprise_id": p.enterprise_id,
                "enterprise_name": p.enterprise_name,
                "predicted_net_credit": p.predicted_net_credit,
                "predicted_compliance_rate": p.predicted_compliance_rate,
                "is_predicted_compliant": p.predicted_net_credit >= 0,
                "predicted_gap_or_surplus": abs(p.predicted_net_credit)
            }
            for p in predictions
        ]
    }


@router.get("/comparison/{enterprise_id}/{start_year}/{end_year}")
def get_historical_vs_predicted(
    enterprise_id: int,
    start_year: int,
    end_year: int,
    output_growth_rate: float = Query(0.05, description="产量年增长率"),
    pc_improvement_rate: float = Query(0.02, description="电耗年改善率"),
    db: Session = Depends(get_db)
):
    enterprise = crud.get_enterprise(db, enterprise_id=enterprise_id)
    if not enterprise:
        raise HTTPException(status_code=404, detail="企业不存在")

    target_year = end_year + 1

    historical_data = []
    for year in range(start_year, end_year + 1):
        summary = crud.calculate_enterprise_credit_summary(db, enterprise_id, year)
        if summary:
            historical_data.append({
                "year": year,
                "total_positive": summary.total_positive_credit,
                "total_negative": summary.total_negative_credit,
                "net_credit": summary.net_credit,
                "compliance_rate": summary.compliance_rate,
                "credit_gap": summary.credit_gap,
                "credit_surplus": summary.credit_surplus
            })

    prediction = crud.predict_enterprise_credit(
        db,
        enterprise_id=enterprise_id,
        target_year=target_year,
        output_growth_rate=output_growth_rate,
        pc_improvement_rate=pc_improvement_rate
    )

    historical_avg_net = sum(h["net_credit"] for h in historical_data) / len(historical_data) if historical_data else 0

    return {
        "enterprise_id": enterprise_id,
        "enterprise_name": enterprise.name,
        "historical_years": list(range(start_year, end_year + 1)),
        "historical_data": historical_data,
        "historical_average_net_credit": round(historical_avg_net, 2),
        "prediction_year": target_year,
        "prediction": {
            "predicted_total_positive": prediction.predicted_total_positive,
            "predicted_total_negative": prediction.predicted_total_negative,
            "predicted_net_credit": prediction.predicted_net_credit,
            "predicted_compliance_rate": prediction.predicted_compliance_rate
        },
        "trend_analysis": {
            "direction": "upward" if prediction.predicted_net_credit > historical_avg_net else "downward" if prediction.predicted_net_credit < historical_avg_net else "stable",
            "change_percentage": round(((prediction.predicted_net_credit - historical_avg_net) / abs(historical_avg_net) * 100), 2) if historical_avg_net != 0 else 0
        }
    }
