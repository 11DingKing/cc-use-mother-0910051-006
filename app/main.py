from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import Base, engine
from .routers import enterprises, vehicle_models, credit_records, credit_transactions, statistics
from .routers import credit_market, credit_carryover, credit_prediction, credit_batches

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.APP_NAME,
    description="工信部双积分核算与电耗限值管理系统 - 用于管理新能源汽车企业双积分核算、电耗限值标准、积分交易撮合等业务。新增功能：积分交易市场（挂单交易、价格走势）、跨年度结转、积分预测、积分批次台账（批次拆分、可解释选择、来源链、规则重放、数量守恒）。",
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(enterprises.router, prefix=settings.API_V1_PREFIX)
app.include_router(vehicle_models.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_records.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_transactions.router, prefix=settings.API_V1_PREFIX)
app.include_router(statistics.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_market.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_carryover.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_prediction.router, prefix=settings.API_V1_PREFIX)
app.include_router(credit_batches.router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["root"])
def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.VERSION,
        "message": "欢迎使用双积分核算与电耗限值管理系统",
        "docs": "/docs",
        "api_prefix": settings.API_V1_PREFIX
    }


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "healthy"}
