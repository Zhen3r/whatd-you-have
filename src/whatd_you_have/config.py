from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    kimi_api_key: str
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_vision_model: str = "kimi-k2.5"
    kimi_text_model: str = "moonshot-v1-8k"

    wechatbot_cred_path: str = "./data/wechatbot_credentials.json"

    database_path: str = "./data/whatd_you_have.db"
    timezone: str = "Asia/Shanghai"
    daily_summary_hour: int = 21
    daily_summary_minute: int = 0

    nag_after_hours: int = 5
    nag_interval_minutes: int = 45
    nag_start_hour: int = 8
    nag_end_hour: int = 23

    default_daily_calorie_goal: int = 1800
    image_caption_wait_secs: int = 30


settings = Settings()  # type: ignore[call-arg]
