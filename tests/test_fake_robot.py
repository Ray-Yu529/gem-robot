from __future__ import annotations

from dataclasses import replace

import pytest

from fake_robot import DEFAULT_CONFIG_PATH, FakeRobot, GotoResult, ManualClock, Reason, RobotConfig, RobotState

CONFIG = RobotConfig(
    stations=("A", "B", "CHARGER"),
    start_station="CHARGER",
    move_seconds=5.0,
    battery_initial=100.0,
    drain_per_second=2.0,
    low_threshold=20.0,
)


def make(**overrides):
    clock = ManualClock()
    robot = FakeRobot(replace(CONFIG, **overrides), clock=clock)
    seen = []
    robot.add_listener(seen.append)
    return clock, robot, seen


def test_config_file_has_required_stations():
    config = RobotConfig.from_file(DEFAULT_CONFIG_PATH)
    assert {"A", "B", "CHARGER"} <= set(config.stations)
    assert len(config.stations) >= 3


def test_goto_is_non_blocking_and_arrives_after_move_seconds():
    clock, robot, seen = make()
    assert robot.state() is RobotState.IDLE
    assert robot.goto("A") is GotoResult.ACCEPTED
    assert robot.state() is RobotState.MOVING
    assert robot.position() is None

    clock.advance(4.9)
    assert robot.state() is RobotState.MOVING
    clock.advance(0.1)
    assert robot.state() is RobotState.IDLE
    assert robot.position() == "A"
    assert [t.reason for t in seen] == [Reason.GOTO, Reason.ARRIVED]
    assert seen[-1].station == "A"


def test_battery_only_drains_while_moving():
    clock, robot, _ = make()
    clock.advance(100)
    assert robot.battery() == 100.0
    robot.goto("A")
    clock.advance(2)
    assert robot.battery() == pytest.approx(96.0)
    clock.advance(50)  # 到站後不再耗電
    assert robot.battery() == pytest.approx(90.0)


def test_goto_rejections():
    clock, robot, _ = make()
    assert robot.goto("NOWHERE") is GotoResult.UNKNOWN_STATION
    assert robot.goto("A") is GotoResult.ACCEPTED
    assert robot.goto("B") is GotoResult.NOT_IDLE
    assert robot.target() == "A"


def test_low_battery_stops_the_move_and_charging_recovers():
    clock, robot, seen = make(battery_initial=25.0)
    robot.goto("A")
    clock.advance(2.4)  # 25 - 4.8 = 20.2，還沒低於門檻
    assert robot.state() is RobotState.MOVING
    clock.advance(0.2)  # 19.8 < 20
    assert robot.state() is RobotState.ALARM
    assert robot.battery() < 20.0

    clock.advance(60)  # 停下後不會到站，也不再耗電
    assert robot.state() is RobotState.ALARM
    assert robot.position() is None
    assert robot.goto("A") is GotoResult.NOT_IDLE

    assert robot.charge() is True
    assert robot.state() is RobotState.IDLE
    assert robot.battery() == 100.0
    assert [t.reason for t in seen] == [Reason.GOTO, Reason.LOW_BATTERY, Reason.CHARGED]
    assert robot.goto("A") is GotoResult.ACCEPTED


def test_cannot_charge_while_moving():
    _, robot, _ = make()
    robot.goto("A")
    assert robot.charge() is False


def test_starts_in_alarm_when_battery_already_low():
    _, robot, _ = make(battery_initial=5.0)
    assert robot.state() is RobotState.ALARM
