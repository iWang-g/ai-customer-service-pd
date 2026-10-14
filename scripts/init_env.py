"""Create local configuration without printing secrets or overwriting existing settings."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = root / ".env"
if destination.exists():
    print(".env 已存在，保留原配置。")
else:
    template = (root / ".env.example").read_text(encoding="utf-8")
    template = template.replace("replace-with-a-long-random-admin-token", secrets.token_urlsafe(32))
    destination.write_text(template, encoding="utf-8")
    print("已创建 .env 和随机管理员令牌。请在本地填写 QQ 与模型凭据。")
