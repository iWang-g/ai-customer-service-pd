from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    qq_app_id: str = ""
    qq_app_secret: str = ""
    qq_transport: Literal["webhook", "websocket"] = "webhook"
    qq_gateway_enabled: bool = False
    qq_gateway_intents: int = Field(default=1 << 25, ge=1, le=2 ** 32 - 1)
    qq_gateway_max_retries: int = Field(default=10, ge=1, le=100)
    qq_api_base: str = "https://api.sgroup.qq.com"
    qq_token_url: str = "https://bots.qq.com/app/getAppAccessToken"
    qq_dry_run: bool = True
    reply_window_seconds: int = Field(default=240, ge=30, le=300)
    c2c_reply_window_seconds: int = Field(default=3300, ge=30, le=3600)
    admin_token: str = ""
    data_dir: Path = Path("data")
    model_base_url: str = ""
    model_api_key: str = ""
    vision_model: str = ""
    chat_model: str = ""
    chat_enabled: bool = False
    auto_apply_ai: bool = False
    ai_confidence_threshold: float = Field(default=.95, ge=0, le=1)
    image_allowed_hosts: str = "multimedia.nt.qq.com,multimedia.nt.qq.com.cn,gchat.qpic.cn,c2cpicdw.qpic.cn"
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    reconcile_days: int = Field(default=14, ge=1, le=15)
    worker_enabled: bool = True
    commerce_base_url: str = ""
    commerce_api_key: str = ""

    @model_validator(mode="after")
    def validate_live(self):
        if self.qq_gateway_enabled:
            if self.qq_transport != "websocket":
                raise ValueError("QQ_GATEWAY_ENABLED 只可用于 websocket 接入")
            if not self.qq_app_id or not self.qq_app_secret:
                raise ValueError("WebSocket 接入需要 QQ_APP_ID 和 QQ_APP_SECRET")
            if not self.worker_enabled:
                raise ValueError("WebSocket 接入需要启用消息处理 worker")
        if not self.qq_dry_run:
            if not self.qq_app_id or not self.qq_app_secret:
                raise ValueError("正式模式需要 QQ_APP_ID 和 QQ_APP_SECRET")
            if len(self.admin_token) < 24 or self.admin_token.startswith("replace-"):
                raise ValueError("正式模式需要至少 24 位随机 ADMIN_TOKEN")
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "orders.sqlite3"
