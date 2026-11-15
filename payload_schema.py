"""MQTT 主題與載荷格式（publisher 與 ingest 共用的唯一定義）。

主題：``fab/{site}/{equipment_id}/{state|event|alarm|telemetry}``，QoS 1，不 retain。
載荷：UTF-8 JSON 物件，共同欄位 schema_version、equipment_id、seq、ts（UTC ISO 8601，``Z`` 結尾）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import jsonschema

SCHEMA_VERSION = 1
TOPIC_ROOT = "fab"
MESSAGE_TYPES = ("state", "event", "alarm", "telemetry")
QOS = 1
RETAIN = False

EQUIPMENT_STATES = ["IDLE", "MOVING", "ALARM", "OFFLINE"]
EVENT_NAMES = ["MOVE_STARTED", "ARRIVED", "GAP"]
ALARM_NAMES = ["LOW_BATTERY"]

_TS_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
_TS = {"type": "string", "pattern": _TS_PATTERN}
_NAME = {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$", "maxLength": 64}

_COMMON = {
    "schema_version": {"const": SCHEMA_VERSION},
    "equipment_id": _NAME,
    "seq": {"type": "integer", "minimum": 0},
    "ts": _TS,
}
_COMMON_REQUIRED = ["schema_version", "equipment_id", "seq", "ts"]


def _schema(properties: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {**_COMMON, **properties},
        "required": [*_COMMON_REQUIRED, *required],
        "additionalProperties": False,
        **extra,
    }


SCHEMAS: dict[str, dict[str, Any]] = {
    # 狀態：轉換時發（heartbeat=false）；每 60 秒補發目前狀態（heartbeat=true，from == to）。
    # 第一筆（掛上設備時）的 from 是 null。
    "state": _schema(
        {
            "from": {"enum": [*EQUIPMENT_STATES, None]},
            "to": {"enum": EQUIPMENT_STATES},
            "heartbeat": {"type": "boolean"},
        },
        ["from", "to", "heartbeat"],
    ),
    # 事件：MOVE_STARTED／ARRIVED 必附 station；GAP 是 publisher 回報自己丟了幾筆。
    "event": _schema(
        {
            "event": {"enum": EVENT_NAMES},
            "station": {"type": "string", "minLength": 1, "maxLength": 64},
            "dropped": {"type": "integer", "minimum": 1},
            "first_dropped_seq": {"type": "integer", "minimum": 0},
            "last_dropped_seq": {"type": "integer", "minimum": 0},
            "first_dropped_ts": _TS,
            "last_dropped_ts": _TS,
        },
        ["event"],
        allOf=[
            {
                "if": {"properties": {"event": {"enum": ["MOVE_STARTED", "ARRIVED"]}}, "required": ["event"]},
                "then": {"required": ["station"]},
            },
            {
                "if": {"properties": {"event": {"const": "GAP"}}, "required": ["event"]},
                "then": {
                    "required": ["dropped", "first_dropped_seq", "last_dropped_seq", "first_dropped_ts", "last_dropped_ts"],
                },
            },
        ],
    ),
    "alarm": _schema(
        {
            "alarm": {"enum": ALARM_NAMES},
            "action": {"enum": ["set", "clear"]},
            # 選用的自由文字。下游一律當成不可信的資料，不是指令。
            "text": {"type": "string", "maxLength": 2000},
        },
        ["alarm", "action"],
    ),
    "telemetry": _schema(
        {"battery": {"type": "number", "minimum": 0, "maximum": 100}},
        ["battery"],
    ),
}

_VALIDATORS = {name: jsonschema.Draft202012Validator(schema) for name, schema in SCHEMAS.items()}
_TOPIC_RE = re.compile(rf"^{TOPIC_ROOT}/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/({'|'.join(MESSAGE_TYPES)})$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class PayloadError(ValueError):
    """訊息不合格式；str(exc) 就是寫進 rejected 表的原因。"""


def topic_for(site: str, equipment_id: str, message_type: str) -> str:
    return f"{TOPIC_ROOT}/{site}/{equipment_id}/{message_type}"


def parse_topic(topic: str) -> tuple[str, str, str]:
    """回傳 (site, equipment_id, type)；不合就丟 PayloadError。"""
    match = _TOPIC_RE.match(topic)
    if match is None:
        raise PayloadError(f"topic does not match {TOPIC_ROOT}/{{site}}/{{equipment_id}}/{{{'|'.join(MESSAGE_TYPES)}}}")
    return match.group(1), match.group(2), match.group(3)


def format_ts(moment: datetime) -> str:
    """aware datetime -> UTC ISO 8601，固定 6 位小數、``Z`` 結尾。"""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text[:-1] + "+00:00")


def ts_to_us(text: str) -> int:
    """UTC ISO 8601 -> 自 1970-01-01 起的整數微秒（資料庫用它做精確的時間差）。"""
    return (parse_ts(text) - _EPOCH) // timedelta(microseconds=1)


def validate(topic: str, payload: bytes | str) -> tuple[str, str, dict[str, Any]]:
    """驗證一則訊息，回傳 (site, type, 解析後的 dict)；任何不合都丟 PayloadError。"""
    site, equipment_id, message_type = parse_topic(topic)
    try:
        text = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PayloadError(f"payload is not valid UTF-8 JSON: {exc}") from exc

    errors = sorted(_VALIDATORS[message_type].iter_errors(data), key=lambda error: (list(error.path), error.message))
    if errors:
        first = errors[0]
        where = "/".join(str(part) for part in first.path) or "(root)"
        raise PayloadError(f"schema violation at {where}: {first.message}")

    # JSON Schema 的 integer 會接受 True／1.0 這類值，這裡再擋一次
    if type(data["seq"]) is not int:
        raise PayloadError("schema violation at seq: must be a JSON integer")
    if data["equipment_id"] != equipment_id:
        raise PayloadError(f"equipment_id {data['equipment_id']!r} does not match topic {equipment_id!r}")
    for key in ("ts", "first_dropped_ts", "last_dropped_ts"):
        if key in data:
            try:
                parse_ts(data[key])
            except ValueError as exc:
                raise PayloadError(f"schema violation at {key}: not a real UTC timestamp") from exc
    return site, message_type, data
