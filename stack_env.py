"""讀取 docker compose 用的 .env，組出本機連線參數。只在本機模擬環境使用。"""

from __future__ import annotations

import socket
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"


def load_env(path: str | Path = ENV_PATH) -> dict[str, str]:
    """極簡的 .env 解析（KEY=VALUE，# 開頭是註解）。檔案不存在回空 dict。"""
    values: dict[str, str] = {}
    path = Path(path)
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _dsn(env: dict[str, str], user_key: str, password_key: str) -> str:
    return (
        f"postgresql://{quote(env[user_key], safe='')}:{quote(env[password_key], safe='')}"
        f"@127.0.0.1:{env.get('POSTGRES_PORT', '5432')}/{quote(env.get('POSTGRES_DB', 'gemrobot'), safe='')}"
    )


def owner_dsn(env: dict[str, str]) -> str:
    """擁有者帳號（可寫）：只給 ingest 用。"""
    return _dsn(env, "POSTGRES_USER", "POSTGRES_PASSWORD")


def readonly_dsn(env: dict[str, str]) -> str:
    """唯讀角色：MCP server 用。"""
    return _dsn(env, "READONLY_USER", "READONLY_PASSWORD")


def mqtt_params(env: dict[str, str]) -> dict:
    return {
        "host": "127.0.0.1",
        "port": int(env.get("MQTT_PORT", "1883")),
        "username": env["MQTT_USERNAME"],
        "password": env["MQTT_PASSWORD"],
    }


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
