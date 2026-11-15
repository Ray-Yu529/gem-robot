"""假機器人：確定性的狀態機，時間來自可注入的時鐘。

狀態只在 ``poll()``（以及會先呼叫它的 ``goto/battery/state/charge``）時依時鐘推進，
本身不開執行緒、不 sleep，所以測試可以用 ``ManualClock`` 直接撥時間。
"""

from __future__ import annotations

import enum
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "stations.json"


class RobotState(str, enum.Enum):
    IDLE = "IDLE"
    MOVING = "MOVING"
    ALARM = "ALARM"


class GotoResult(enum.Enum):
    ACCEPTED = "ACCEPTED"
    UNKNOWN_STATION = "UNKNOWN_STATION"
    NOT_IDLE = "NOT_IDLE"


class Reason(str, enum.Enum):
    """狀態轉換的原因。"""

    GOTO = "GOTO"  # IDLE -> MOVING
    ARRIVED = "ARRIVED"  # MOVING -> IDLE
    LOW_BATTERY = "LOW_BATTERY"  # MOVING -> ALARM（移動中止）
    CHARGED = "CHARGED"  # ALARM -> IDLE


@dataclass(frozen=True)
class Transition:
    old: RobotState
    new: RobotState
    reason: Reason
    station: str | None  # GOTO/LOW_BATTERY：目的站；ARRIVED：抵達站；CHARGED：None
    battery: float
    at: float  # 轉換被偵測到時的時鐘讀數


@dataclass(frozen=True)
class RobotConfig:
    stations: tuple[str, ...]
    start_station: str | None = None
    move_seconds: float = 5.0
    battery_initial: float = 100.0
    battery_full: float = 100.0
    drain_per_second: float = 2.0
    low_threshold: float = 20.0

    def __post_init__(self) -> None:
        if len(set(self.stations)) != len(self.stations) or not self.stations:
            raise ValueError("stations must be a non-empty list of unique names")
        if self.start_station is not None and self.start_station not in self.stations:
            raise ValueError(f"start_station {self.start_station!r} is not in stations")
        if self.move_seconds <= 0 or self.drain_per_second < 0:
            raise ValueError("move_seconds must be > 0 and drain_per_second >= 0")

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> RobotConfig:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        battery = raw.get("battery", {})
        return cls(
            stations=tuple(raw["stations"]),
            start_station=raw.get("start_station"),
            move_seconds=float(raw.get("move_seconds", 5.0)),
            battery_initial=float(battery.get("initial", 100.0)),
            battery_full=float(battery.get("full", 100.0)),
            drain_per_second=float(battery.get("drain_per_second", 2.0)),
            low_threshold=float(battery.get("low_threshold", 20.0)),
        )


class ManualClock:
    """測試用時鐘：只有 ``advance()`` 會讓時間前進。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("time cannot go backwards")
        with self._lock:
            self._now += seconds
            return self._now


class FakeRobot:
    """IDLE / MOVING / ALARM 三態的假機器人。

    - ``goto(station)`` 非阻塞；``move_seconds`` 後到站回 IDLE。
    - 只有移動時耗電（線性）。取樣到電量低於門檻就立刻停下進入 ALARM，該次移動不會到站。
    - ``charge()`` 把電量充滿；若在 ALARM 則回 IDLE（位置未知，直到下次到站）。
    """

    def __init__(self, config: RobotConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Transition], None]] = []
        self._battery = float(config.battery_initial)
        self._position: str | None = config.start_station
        self._target: str | None = None
        self._move_start = 0.0
        self._move_start_battery = 0.0
        self._arrive_at = 0.0
        self._state = RobotState.ALARM if self._battery < config.low_threshold else RobotState.IDLE

    # ---- 觀察 -------------------------------------------------------------

    def add_listener(self, listener: Callable[[Transition], None]) -> None:
        """註冊狀態轉換回呼。回呼在機器人的鎖內同步執行，必須很快且不可阻塞。"""
        with self._lock:
            self._listeners.append(listener)

    def state(self) -> RobotState:
        with self._lock:
            self._advance()
            return self._state

    def battery(self) -> float:
        with self._lock:
            self._advance()
            return self._battery

    def position(self) -> str | None:
        """目前所在站；移動中或中途停下時為 None。"""
        with self._lock:
            self._advance()
            return self._position

    def target(self) -> str | None:
        with self._lock:
            self._advance()
            return self._target

    def has_station(self, station: object) -> bool:
        return isinstance(station, str) and station in self.config.stations

    # ---- 操作 -------------------------------------------------------------

    def poll(self) -> None:
        """依時鐘推進模擬；有狀態轉換就通知 listener。"""
        with self._lock:
            self._advance()

    def goto(self, station: str) -> GotoResult:
        with self._lock:
            self._advance()
            if not self.has_station(station):
                return GotoResult.UNKNOWN_STATION
            if self._state is not RobotState.IDLE:
                return GotoResult.NOT_IDLE
            now = self._clock()
            self._target = station
            self._position = None
            self._move_start = now
            self._move_start_battery = self._battery
            self._arrive_at = now + self.config.move_seconds
            self._transition(RobotState.MOVING, Reason.GOTO, station, now)
            return GotoResult.ACCEPTED

    def charge(self) -> bool:
        """充滿電。移動中不可充電（回 False）。"""
        with self._lock:
            self._advance()
            if self._state is RobotState.MOVING:
                return False
            self._battery = float(self.config.battery_full)
            if self._state is RobotState.ALARM and self._battery >= self.config.low_threshold:
                self._transition(RobotState.IDLE, Reason.CHARGED, None, self._clock())
            return True

    # ---- 內部 -------------------------------------------------------------

    def _advance(self) -> None:
        if self._state is not RobotState.MOVING:
            return
        now = self._clock()
        effective = min(now, self._arrive_at)
        drained = self.config.drain_per_second * (effective - self._move_start)
        self._battery = max(0.0, self._move_start_battery - drained)
        target = self._target
        if self._battery < self.config.low_threshold:
            self._target = None
            self._transition(RobotState.ALARM, Reason.LOW_BATTERY, target, now)
        elif now >= self._arrive_at:
            self._position = target
            self._target = None
            self._transition(RobotState.IDLE, Reason.ARRIVED, target, now)

    def _transition(self, new: RobotState, reason: Reason, station: str | None, at: float) -> None:
        transition = Transition(self._state, new, reason, station, self._battery, at)
        self._state = new
        for listener in list(self._listeners):
            listener(transition)
