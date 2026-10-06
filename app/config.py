from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "实验室样品流转系统"
    # Compose 中数据库主机名为 db；本地可用 .env 覆盖
    database_url: str = "postgresql+psycopg2://lab:labpass@db:5432/labflow"
    seed_default_staff: bool = True
    seed_demo_data: bool = True


settings = Settings()
