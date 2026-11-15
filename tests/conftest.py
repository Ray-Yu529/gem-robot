from __future__ import annotations

import socket
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from fake_robot import FakeRobot, ManualClock, RobotConfig
from gem_equipment import RobotGemEquipment
from history import History
from host_client import HostClient, stop_pair

BASE_CONFIG = RobotConfig(
    stations=("A", "B", "CHARGER"),
    start_station="CHARGER",
    move_seconds=5.0,
    battery_initial=100.0,
    battery_full=100.0,
    drain_per_second=2.0,
    low_threshold=20.0,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Rig:
    clock: ManualClock
    robot: FakeRobot
    history: History
    equipment: RobotGemEquipment
    host: HostClient

    def advance(self, seconds: float) -> None:
        """撥動假時鐘並讓設備取樣一次機器人（不 sleep）。"""
        self.clock.advance(seconds)
        self.equipment.poll_robot()

    def go_online_remote(self) -> None:
        assert self.host.go_online() == 0
        assert self.equipment.control_state_name == "ONLINE_REMOTE"
        assert self.host.enable_move_events() == (0, 0, 0)


@dataclass(frozen=True)
class SimData:
    db: Path
    history: Path
    summary: dict


@pytest.fixture(scope="session")
def sim_data(tmp_path_factory) -> SimData:
    """整個測試 session 共用的一份 24 小時模擬資料（含對抗性警報文字）。唯讀使用，不要改它。"""
    from simulate_day import run_simulation  # noqa: PLC0415

    folder = tmp_path_factory.mktemp("sim")
    summary = run_simulation(folder / "sim.sqlite", hours=24, adversarial=True)
    return SimData(folder / "sim.sqlite", Path(summary["history"]), summary)


@pytest.fixture
def make_rig(tmp_path):
    rigs: list[Rig] = []

    def _make(**config_overrides) -> Rig:
        clock = ManualClock()
        robot = FakeRobot(replace(BASE_CONFIG, **config_overrides), clock=clock)
        history = History(tmp_path / f"history_{len(rigs)}.jsonl")
        port = free_port()
        # poll_interval=None：不開輪詢執行緒，由測試在撥時鐘後明確呼叫 poll_robot()
        equipment = RobotGemEquipment(robot, history, port=port, poll_interval=None)
        host = HostClient(port=port)
        rig = Rig(clock, robot, history, equipment, host)
        rigs.append(rig)
        equipment.start()
        host.start()
        assert host.wait_communicating(10), "host did not reach COMMUNICATING"
        assert equipment.waitfor_communicating(10), "equipment did not reach COMMUNICATING"
        return rig

    yield _make

    for rig in rigs:
        stop_pair(rig.equipment, rig.host)


@pytest.fixture
def rig(make_rig) -> Rig:
    return make_rig()
