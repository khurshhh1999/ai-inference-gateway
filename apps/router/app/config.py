from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    provider_mode: str = "mock"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "info"
    port: int = 8081
    mock_latency_ms: int = 40


settings = Settings()
