from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional, Dict
from datetime import datetime

from .. import crud, schemas
from ..database import get_db
from ..models import OrderType, OrderStatus

router = APIRouter(prefix="/credit-market", tags=["credit-market"])


@router.post("/orders", response_model=schemas.CreditOrder)
def create_order(
    order: schemas.CreditOrderCreate,
    db: Session = Depends(get_db)
):
    try:
        return crud.create_credit_order(db=db, order=order)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders", response_model=List[schemas.CreditOrderWithDetail])
def read_orders(
    enterprise_id: Optional[int] = None,
    year: Optional[int] = None,
    order_type: Optional[OrderType] = None,
    status: Optional[OrderStatus] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    orders = crud.get_credit_orders(
        db,
        enterprise_id=enterprise_id,
        year=year,
        order_type=order_type,
        status=status,
        skip=skip,
        limit=limit
    )
    return orders


@router.get("/orders/{order_id}", response_model=schemas.CreditOrderWithDetail)
def read_order(order_id: int, db: Session = Depends(get_db)):
    order = crud.get_credit_order(db, order_id=order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@router.put("/orders/{order_id}", response_model=schemas.CreditOrder)
def update_order(
    order_id: int,
    order_update: schemas.CreditOrderUpdate,
    db: Session = Depends(get_db)
):
    try:
        order = crud.update_credit_order(db, order_id=order_id, order_update=order_update)
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        return order
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/orders/{order_id}/cancel", response_model=schemas.CreditOrder)
def cancel_order(order_id: int, db: Session = Depends(get_db)):
    try:
        order = crud.cancel_credit_order(db, order_id=order_id)
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        return order
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/match", response_model=schemas.CreditTransactionWithDetail)
def match_specific_orders(
    match_request: schemas.CreditOrderMatchRequest,
    db: Session = Depends(get_db)
):
    transaction, error = crud.match_orders_by_id(db, match_request)
    if error:
        raise HTTPException(status_code=400, detail=error)
    if not transaction:
        raise HTTPException(status_code=400, detail="撮合失败")
    return transaction


@router.post("/match-all/{year}", response_model=schemas.MatchWithOrdersResponse)
def match_all_orders(year: int, db: Session = Depends(get_db)):
    transactions, matched_orders, remaining_gap, remaining_surplus = crud.match_all_pending_orders(
        db, year=year
    )

    if not transactions:
        return schemas.MatchWithOrdersResponse(
            success=True,
            message="当前没有可撮合的挂单",
            transactions=[],
            matched_orders=[],
            remaining_gap=remaining_gap,
            remaining_surplus=remaining_surplus
        )

    return schemas.MatchWithOrdersResponse(
        success=True,
        message=f"成功完成 {len(transactions)} 笔挂单撮合交易",
        transactions=transactions,
        matched_orders=matched_orders,
        remaining_gap=remaining_gap,
        remaining_surplus=remaining_surplus
    )


@router.get("/price-history", response_model=List[schemas.PriceHistory])
def read_price_history(
    year: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    return crud.get_price_history(
        db,
        year=year,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit
    )


@router.get("/price-trend/{year}", response_model=schemas.PriceTrendResponse)
def get_price_trend(year: int, db: Session = Depends(get_db)):
    return crud.get_price_trend(db, year=year)


@router.get("/overview/{year}", response_model=schemas.MarketOverviewResponse)
def get_market_overview(year: int, db: Session = Depends(get_db)):
    return crud.get_market_overview(db, year=year)


@router.get("/sell-order-book/{year}")
def get_sell_order_book(
    year: int,
    db: Session = Depends(get_db)
):
    orders = crud.get_credit_orders(
        db,
        year=year,
        order_type=OrderType.SELL,
        status=OrderStatus.PENDING,
        limit=1000
    )
    partial_orders = crud.get_credit_orders(
        db,
        year=year,
        order_type=OrderType.SELL,
        status=OrderStatus.PARTIAL,
        limit=1000
    )
    all_orders = orders + partial_orders

    price_levels: Dict[float, float] = {}
    for o in all_orders:
        if o.remaining_amount <= 0.01:
            continue
        price = o.unit_price
        if price not in price_levels:
            price_levels[price] = 0.0
        price_levels[price] += o.remaining_amount

    order_book = [
        {"price": price, "volume": round(vol, 2)}
        for price, vol in sorted(price_levels.items())
    ]

    return {
        "year": year,
        "order_type": "sell",
        "order_book": order_book,
        "total_volume": round(sum(price_levels.values()), 2),
        "price_levels": len(price_levels)
    }


@router.get("/buy-order-book/{year}")
def get_buy_order_book(
    year: int,
    db: Session = Depends(get_db)
):
    orders = crud.get_credit_orders(
        db,
        year=year,
        order_type=OrderType.BUY,
        status=OrderStatus.PENDING,
        limit=1000
    )
    partial_orders = crud.get_credit_orders(
        db,
        year=year,
        order_type=OrderType.BUY,
        status=OrderStatus.PARTIAL,
        limit=1000
    )
    all_orders = orders + partial_orders

    price_levels: Dict[float, float] = {}
    for o in all_orders:
        if o.remaining_amount <= 0.01:
            continue
        price = o.unit_price
        if price not in price_levels:
            price_levels[price] = 0.0
        price_levels[price] += o.remaining_amount

    order_book = [
        {"price": price, "volume": round(vol, 2)}
        for price, vol in sorted(price_levels.items(), reverse=True)
    ]

    return {
        "year": year,
        "order_type": "buy",
        "order_book": order_book,
        "total_volume": round(sum(price_levels.values()), 2),
        "price_levels": len(price_levels)
    }
