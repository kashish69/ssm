from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    domain: str

    db_path: str = "/data/app.db"

    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str = "us-east-1"
    s3_bucket: str
    s3_prefix: str = ""
    presigned_url_ttl_seconds: int = 600

    app_passkeys: str = ""   # comma-separated, seeded via migration
    device_seeds: str = ""   # device_id:display_name:api_key triples, ";" separated
    session_secret: str
    session_cookie_name: str = "session"
    session_max_age_seconds: int = 7 * 24 * 3600

    mqtt_broker_host: str
    mqtt_broker_port: int = 8883
    mqtt_use_tls: bool = True
    mqtt_tls_ca_cert: str | None = None
    mqtt_service_username: str = "backend-service"
    mqtt_service_password: str

    capture_timeout_seconds: int = 25
    capture_cooldown_seconds: int = 10
    upload_max_bytes: int = 10 * 1024 * 1024

    class Config:
        env_file = ".env"


settings = Settings()
