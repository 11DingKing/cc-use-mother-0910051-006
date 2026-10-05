from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "双积分核算与电耗限值管理系统"
    VERSION: str = "1.0.0"
    DATABASE_URL: str = "sqlite:///./dual_credit.db"
    API_V1_PREFIX: str = "/api/v1"

    class Config:
        env_file = ".env"


settings = Settings()
