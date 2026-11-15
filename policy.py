"""載入 policy.yaml 成為不可變物件，並算出檔案的 SHA-256。

只有「載入」這一個入口；沒有重載、沒有修改、沒有寫檔。格式不對就丟例外（閘門起不來，比帶著錯的政策跑安全）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    version: int
    sha256: str
    equipment: frozenset[str]
    allowed_rcmd: frozenset[str]
    auto_stations: frozenset[str]
    escalate_stations: frozenset[str]
    required_control_state: str
    required_robot_state: str
    battery_safety_margin: float
    rate_window_seconds: float
    rate_max_moves: int
    duplicate_window_seconds: float
    approval_ttl_seconds: float
    snapshot_max_age_seconds: float


def _section(data: dict, key: str, allowed: set[str]) -> dict:
    section = data.get(key)
    if not isinstance(section, dict) or set(section) != allowed:
        raise PolicyError(f"policy section {key!r} must have exactly the keys {sorted(allowed)}")
    return section


def _names(value: object, what: str, allow_empty: bool = False) -> frozenset[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise PolicyError(f"{what} must be a list of names")
    for item in value:
        if not isinstance(item, str) or not _NAME.match(item):
            raise PolicyError(f"{what}: {item!r} is not a valid name")
    if len(set(value)) != len(value):
        raise PolicyError(f"{what} has duplicates")
    return frozenset(value)


def _positive(value: object, what: str, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)) or value <= 0:
        raise PolicyError(f"{what} must be a positive {'integer' if integer else 'number'}")
    return value


def parse_policy(raw: bytes) -> Policy:
    data = yaml.safe_load(raw.decode("utf-8"))
    top = {"version", "equipment", "allowed_rcmd", "stations", "preconditions", "rate_limit", "duplicate", "approval", "snapshot"}
    if not isinstance(data, dict) or set(data) != top:
        raise PolicyError(f"policy must have exactly the top-level keys {sorted(top)}")
    if data["version"] != 1:
        raise PolicyError("unsupported policy version")
    stations = _section(data, "stations", {"auto", "escalate"})
    pre = _section(data, "preconditions", {"control_state", "robot_state", "battery_safety_margin"})
    rate = _section(data, "rate_limit", {"window_seconds", "max_moves"})
    auto = _names(stations["auto"], "stations.auto")
    escalate = _names(stations["escalate"], "stations.escalate", allow_empty=True)
    if auto & escalate:
        raise PolicyError("a station cannot be both auto and escalate")
    margin = pre["battery_safety_margin"]
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not 0 <= margin <= 100:
        raise PolicyError("battery_safety_margin must be between 0 and 100")
    return Policy(
        version=1,
        sha256=hashlib.sha256(raw).hexdigest(),
        equipment=_names(data["equipment"], "equipment"),
        allowed_rcmd=_names(data["allowed_rcmd"], "allowed_rcmd"),
        auto_stations=auto,
        escalate_stations=escalate,
        required_control_state=str(pre["control_state"]),
        required_robot_state=str(pre["robot_state"]),
        battery_safety_margin=float(margin),
        rate_window_seconds=float(_positive(rate["window_seconds"], "rate_limit.window_seconds")),
        rate_max_moves=int(_positive(rate["max_moves"], "rate_limit.max_moves", integer=True)),
        duplicate_window_seconds=float(_positive(_section(data, "duplicate", {"window_seconds"})["window_seconds"], "duplicate.window_seconds")),
        approval_ttl_seconds=float(_positive(_section(data, "approval", {"ttl_seconds"})["ttl_seconds"], "approval.ttl_seconds")),
        snapshot_max_age_seconds=float(_positive(_section(data, "snapshot", {"max_age_seconds"})["max_age_seconds"], "snapshot.max_age_seconds")),
    )


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> Policy:
    return parse_policy(Path(path).read_bytes())
