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
    # The UI holds the modal open while this runs, so it's a live action the
    # user waits on. Must cover the agent's full worst case: WIFI_MAX_ATTEMPTS
    # (3, pi_agent.py) retry cycles, each up to a rescan (~13s) + a stalled
    # nmcli attempt (WIFI_CONNECT_TIMEOUT=25s) = ~38s/cycle, ~114s worst case.
    # Confirmed on hardware: a genuine success settled at 90s — past the prior
    # 50s cap, which showed a false "no confirmation" on a request that
    # actually succeeded. 130s gives headroom above the ~114s worst case.
    wifi_timeout_seconds: int = 130
    upload_max_bytes: int = 10 * 1024 * 1024

    class Config:
        env_file = ".env"


settings = Settings()
