"""Read-only localhost smoke check, with credentials loaded locally."""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qqbot.config import Settings

settings = Settings()
base = "http://127.0.0.1:8000"
for path in ("/healthz", "/admin/status", "/admin/export.xlsx"):
    headers = {"Authorization": "Bearer " + settings.admin_token} if path.startswith("/admin/") else {}
    request = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        if path.endswith("xlsx"):
            assert body.startswith(b"PK")
            print("Excel download: HTTP 200, valid XLSX archive")
        else:
            data = json.loads(body)
            if path == "/healthz":
                print("Health:", data)
            else:
                assert data["worker_enabled"]
                assert not data["report_error"]
                print("Admin: HTTP 200, worker enabled, export healthy, dry_run=", data["dry_run"])
