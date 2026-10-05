from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from .. import crud, schemas
from ..database import get_db
from ..rules import (
    calculate_power_consumption_limit,
    get_weight_suggestion,
    detect_weight_manipulation
)

router = APIRouter(prefix="/statistics", tags=["statistics"])


@router.get("/enterprise-credits/{year}", response_model=List[schemas.EnterpriseCreditSummaryResponse])
def get_enterprise_credit_summaries(year: int, db: Session = Depends(get_db)):
    summaries = crud.calculate_all_enterprise_summaries(db, year=year)
    return [
        schemas.EnterpriseCreditSummaryResponse(
            enterprise_id=s.enterprise_id,
            enterprise_name=s.enterprise_name,
            total_positive_credit=s.total_positive_credit,
            total_negative_credit=s.total_negative_credit,
            net_credit=s.net_credit,
            required_credit=s.required_credit,
            credit_gap=s.credit_gap,
            credit_surplus=s.credit_surplus,
            compliance_rate=s.compliance_rate,
            average_power_consumption=s.average_power_consumption,
            weighted_power_consumption=s.weighted_power_consumption,
            model_count=s.model_count,
            compliant_model_count=s.compliant_model_count
        )
        for s in summaries
    ]


@router.get("/enterprise/{enterprise_id}/{year}", response_model=schemas.EnterpriseCreditSummaryResponse)
def get_single_enterprise_credit_summary(
    enterprise_id: int, year: int, db: Session = Depends(get_db)
):
    summary = crud.calculate_enterprise_credit_summary(db, enterprise_id=enterprise_id, year=year)
    if not summary:
        raise HTTPException(status_code=404, detail="企业不存在")
    return schemas.EnterpriseCreditSummaryResponse(
        enterprise_id=summary.enterprise_id,
        enterprise_name=summary.enterprise_name,
        total_positive_credit=summary.total_positive_credit,
        total_negative_credit=summary.total_negative_credit,
        net_credit=summary.net_credit,
        required_credit=summary.required_credit,
        credit_gap=summary.credit_gap,
        credit_surplus=summary.credit_surplus,
        compliance_rate=summary.compliance_rate,
        average_power_consumption=summary.average_power_consumption,
        weighted_power_consumption=summary.weighted_power_consumption,
        model_count=summary.model_count,
        compliant_model_count=summary.compliant_model_count
    )


@router.get("/enterprise-stats/{year}", response_model=List[schemas.EnterpriseStatsResponse])
def get_enterprise_stats(year: int, db: Session = Depends(get_db)):
    return crud.get_enterprise_stats(db, year=year)


@router.get("/suspicious-models", response_model=List[schemas.SuspiciousModelResponse])
def get_suspicious_weight_models(db: Session = Depends(get_db)):
    models = crud.get_suspicious_weight_models(db)
    result = []
    for m in models:
        limit = calculate_power_consumption_limit(m.curb_weight)
        weight_analysis = get_weight_suggestion(m.curb_weight)
        result.append(schemas.SuspiciousModelResponse(
            id=m.id,
            model_name=m.model_name,
            model_code=m.model_code,
            enterprise_name=m.enterprise.name,
            curb_weight=m.curb_weight,
            power_consumption=m.power_consumption,
            range=m.range,
            power_consumption_limit=limit,
            annual_output=m.annual_output,
            weight_analysis=weight_analysis
        ))
    return result


@router.get("/compliance-ranking/{year}")
def get_compliance_ranking(year: int, db: Session = Depends(get_db)):
    stats = crud.get_enterprise_stats(db, year=year)
    stats_sorted = sorted(stats, key=lambda x: x.compliance_rate, reverse=True)
    return [
        {
            "rank": i + 1,
            "enterprise_id": s.enterprise_id,
            "enterprise_name": s.enterprise_name,
            "compliance_rate": s.compliance_rate,
            "model_count": s.model_count,
            "compliant_model_count": int(s.model_count * s.compliance_rate / 100),
            "net_credit": s.net_credit
        }
        for i, s in enumerate(stats_sorted)
    ]


@router.get("/power-consumption-ranking/{year}")
def get_power_consumption_ranking(year: int, db: Session = Depends(get_db)):
    stats = crud.get_enterprise_stats(db, year=year)
    stats_sorted = sorted(stats, key=lambda x: x.weighted_power_consumption)
    return [
        {
            "rank": i + 1,
            "enterprise_id": s.enterprise_id,
            "enterprise_name": s.enterprise_name,
            "weighted_power_consumption": s.weighted_power_consumption,
            "average_power_consumption": s.average_power_consumption,
            "average_power_consumption_limit": s.average_power_consumption_limit,
            "total_output": s.total_output
        }
        for i, s in enumerate(stats_sorted)
    ]


@router.get("/overview/{year}")
def get_system_overview(year: int, db: Session = Depends(get_db)):
    enterprises = crud.get_enterprises(db)
    models = crud.get_vehicle_models(db)
    stats = crud.get_enterprise_stats(db, year=year)
    summaries = crud.calculate_all_enterprise_summaries(db, year=year)
    transactions = crud.get_credit_transactions(db)
    suspicious_models = crud.get_suspicious_weight_models(db)

    total_output = sum(m.annual_output for m in models)
    total_positive = sum(s.total_positive_credit for s in summaries)
    total_negative = sum(s.total_negative_credit for s in summaries)
    total_gap = sum(s.credit_gap for s in summaries)
    total_surplus = sum(s.credit_surplus for s in summaries)
    compliant_enterprises = sum(1 for s in summaries if s.credit_gap <= 0)

    overall_compliance_rate = (
        sum(s.compliant_model_count for s in summaries) / sum(s.model_count for s in summaries) * 100
        if sum(s.model_count for s in summaries) > 0 else 0
    )

    return {
        "year": year,
        "total_enterprises": len(enterprises),
        "compliant_enterprises": compliant_enterprises,
        "non_compliant_enterprises": len(enterprises) - compliant_enterprises,
        "total_models": len(models),
        "suspicious_models": len(suspicious_models),
        "total_annual_output": total_output,
        "total_positive_credit": round(total_positive, 2),
        "total_negative_credit": round(total_negative, 2),
        "total_net_credit": round(total_positive + total_negative, 2),
        "total_credit_gap": round(total_gap, 2),
        "total_credit_surplus": round(total_surplus, 2),
        "overall_compliance_rate": round(overall_compliance_rate, 2),
        "total_transactions": len(transactions),
        "transaction_amount": round(sum(t.total_amount for t in transactions if t.total_amount), 2),
        "enterprise_credits": [
            {
                "enterprise_id": s.enterprise_id,
                "enterprise_name": s.enterprise_name,
                "net_credit": s.net_credit,
                "credit_gap": s.credit_gap,
                "credit_surplus": s.credit_surplus,
                "is_compliant": s.credit_gap <= 0
            }
            for s in summaries
        ]
    }


@router.get("/overview-v2/{year}")
def get_system_overview_v2(year: int, db: Session = Depends(get_db)):
    enterprises = crud.get_enterprises(db)
    models = crud.get_vehicle_models(db)
    summaries = crud.calculate_all_enterprise_summaries_v2(db, year=year)
    transactions = crud.get_credit_transactions(db)
    suspicious_models = crud.get_suspicious_weight_models(db)

    total_output = sum(m.annual_output for m in models if m.production_year == year)
    total_positive = sum(s.total_positive_credit for s in summaries)
    total_negative = sum(s.total_negative_credit for s in summaries)
    total_carryover_in = sum(s.carryover_in for s in summaries)
    total_carryover_out = sum(s.carryover_out for s in summaries)
    total_bought = sum(s.bought_credit for s in summaries)
    total_sold = sum(s.sold_credit for s in summaries)
    total_final_net = sum(s.final_net_credit for s in summaries)
    total_final_gap = sum(s.final_credit_gap for s in summaries)
    total_final_surplus = sum(s.final_credit_surplus for s in summaries)
    compliant_enterprises = sum(1 for s in summaries if s.is_compliant)

    overall_compliance_rate = (
        sum(s.compliant_model_count for s in summaries) / sum(s.model_count for s in summaries) * 100
        if sum(s.model_count for s in summaries) > 0 else 0
    )

    year_transactions = [t for t in transactions if t.transaction_date and t.transaction_date.year == year]

    return {
        "year": year,
        "total_enterprises": len(enterprises),
        "compliant_enterprises": compliant_enterprises,
        "non_compliant_enterprises": len(enterprises) - compliant_enterprises,
        "total_models": len([m for m in models if m.production_year == year]),
        "suspicious_models": len(suspicious_models),
        "total_annual_output": total_output,
        "total_positive_credit": round(total_positive, 2),
        "total_negative_credit": round(total_negative, 2),
        "total_net_credit": round(total_positive + total_negative, 2),
        "total_carryover_in": round(total_carryover_in, 2),
        "total_carryover_out": round(total_carryover_out, 2),
        "total_bought_credit": round(total_bought, 2),
        "total_sold_credit": round(total_sold, 2),
        "total_final_net_credit": round(total_final_net, 2),
        "total_final_credit_gap": round(total_final_gap, 2),
        "total_final_credit_surplus": round(total_final_surplus, 2),
        "overall_compliance_rate": round(overall_compliance_rate, 2),
        "total_transactions": len(year_transactions),
        "transaction_amount": round(sum(t.total_amount for t in year_transactions if t.total_amount), 2),
        "enterprise_credits": [
            {
                "enterprise_id": s.enterprise_id,
                "enterprise_name": s.enterprise_name,
                "total_positive_credit": s.total_positive_credit,
                "total_negative_credit": s.total_negative_credit,
                "net_credit": s.net_credit,
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
    }
