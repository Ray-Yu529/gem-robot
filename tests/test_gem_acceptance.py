"""驗收測試：真的走 localhost HSMS，主機與設備都是 secsgem；機器人時間用注入時鐘。"""

from __future__ import annotations

import json

import gem_ids
from fake_robot import RobotState
from host_client import HostAlarm, HostEvent

HCACK_INVALID_COMMAND = 1
HCACK_CANNOT_PERFORM_NOW = 2
HCACK_PARAMETER_INVALID = 3
HCACK_ACCEPTED_FINISH_LATER = 4


def test_s1f13_establishes_communication_and_starts_offline(rig):
    # fixture 已經等到雙方 COMMUNICATING（S1F13/S1F14 完成）
    assert rig.equipment.control_state_name == "HOST_OFFLINE"
    sent = [r["data"]["sf"] for r in rig.history.records() if r["kind"] == "secs_message"]
    assert "S1F13" in sent and "S1F14" in sent


def test_legal_move_returns_4_then_move_started_then_arrived(rig):
    rig.go_online_remote()

    assert rig.host.move("A") == HCACK_ACCEPTED_FINISH_LATER
    started = rig.host.wait_for_event(gem_ids.CEID_MOVE_STARTED)
    assert started is not None and started.values == {gem_ids.DVID_STATION: "A"}
    assert rig.robot.state() is RobotState.MOVING

    rig.advance(4.0)  # 還沒到
    assert rig.equipment.wait_idle()
    assert [event.name for event in rig.host.events()] == ["MOVE_STARTED"]

    rig.advance(1.0)
    arrived = rig.host.wait_for_event(gem_ids.CEID_ARRIVED)
    assert arrived is not None and arrived.values == {gem_ids.DVID_STATION: "A"}

    assert [event.name for event in rig.host.events()] == ["MOVE_STARTED", "ARRIVED"]
    assert rig.robot.state() is RobotState.IDLE
    assert rig.robot.position() == "A"


def test_move_while_moving_returns_2(rig):
    rig.go_online_remote()
    assert rig.host.move("A") == HCACK_ACCEPTED_FINISH_LATER
    assert rig.host.move("B") == HCACK_CANNOT_PERFORM_NOW
    assert rig.host.move("A") == HCACK_CANNOT_PERFORM_NOW
    assert rig.robot.target() == "A"

    rig.advance(5.0)
    assert rig.host.wait_for_event(gem_ids.CEID_ARRIVED) is not None
    assert rig.host.move("B") == HCACK_ACCEPTED_FINISH_LATER  # 到站後可以再下


def test_unknown_station_returns_3(rig):
    rig.go_online_remote()
    assert rig.host.move("NOWHERE") == HCACK_PARAMETER_INVALID
    assert rig.host.send_remote_command("MOVE", []) == HCACK_PARAMETER_INVALID
    assert rig.host.send_remote_command("MOVE", [("SPEED", "A")]) == HCACK_PARAMETER_INVALID
    assert rig.robot.state() is RobotState.IDLE


def test_unknown_rcmd_returns_1(rig):
    rig.go_online_remote()
    assert rig.host.send_remote_command("DANCE", [("STATION", "A")]) == HCACK_INVALID_COMMAND
    assert rig.host.send_remote_command("START", []) == HCACK_INVALID_COMMAND
    assert rig.robot.state() is RobotState.IDLE


def test_move_when_offline_returns_2(rig):
    assert rig.equipment.control_state_name == "HOST_OFFLINE"
    assert rig.host.move("A") == HCACK_CANNOT_PERFORM_NOW

    rig.go_online_remote()
    rig.equipment.control_switch_offline()  # 現場切 OFF-LINE
    assert rig.equipment.control_state_name == "EQUIPMENT_OFFLINE"
    assert rig.host.move("A") == HCACK_CANNOT_PERFORM_NOW
    assert rig.robot.state() is RobotState.IDLE


def test_move_when_online_local_returns_2_and_remote_accepts_again(rig):
    rig.go_online_remote()
    rig.equipment.control_switch_online_local()
    assert rig.equipment.control_state_name == "ONLINE_LOCAL"
    assert rig.host.move("A") == HCACK_CANNOT_PERFORM_NOW
    assert rig.robot.state() is RobotState.IDLE

    rig.equipment.control_switch_online_remote()
    assert rig.host.move("A") == HCACK_ACCEPTED_FINISH_LATER


def test_low_battery_sends_s5f1_and_charging_sends_clear(make_rig):
    rig = make_rig(battery_initial=25.0)  # 2%/s，門檻 20 -> 移動 2.5 秒後跌破
    rig.go_online_remote()
    assert rig.host.move("A") == HCACK_ACCEPTED_FINISH_LATER
    assert rig.host.wait_for_event(gem_ids.CEID_MOVE_STARTED) is not None

    rig.advance(3.0)
    alarm = rig.host.wait_for_alarm(gem_ids.ALID_LOW_BATTERY, is_set=True)
    assert alarm is not None
    assert alarm.alcd & 0x80, "ALCD bit 8 should be 1 for an alarm that is set"
    assert rig.robot.state() is RobotState.ALARM

    # 移動已中止：再過很久也不會有 ARRIVED；ALARM 中的 MOVE 回 2
    rig.advance(60.0)
    assert rig.equipment.wait_idle()
    assert rig.host.wait_for_event(gem_ids.CEID_ARRIVED, timeout=0) is None
    assert rig.host.move("A") == HCACK_CANNOT_PERFORM_NOW

    assert rig.equipment.charge_robot() is True
    cleared = rig.host.wait_for_alarm(gem_ids.ALID_LOW_BATTERY, is_set=False)
    assert cleared is not None
    assert cleared.alcd & 0x80 == 0

    alarms = [item for item in rig.host.received if isinstance(item, HostAlarm)]
    assert [item.is_set for item in alarms] == [True, False]
    assert rig.host.move("A") == HCACK_ACCEPTED_FINISH_LATER


def test_events_are_not_sent_until_host_enables_them(rig):
    assert rig.host.go_online() == 0  # 沒有 S2F33/35/37
    assert rig.host.move("A") == HCACK_ACCEPTED_FINISH_LATER
    rig.advance(5.0)
    assert rig.equipment.wait_idle()
    assert rig.host.events() == []
    skipped = [r["data"]["name"] for r in rig.history.records() if r["kind"] == "event" and not r["data"]["reported"]]
    assert "MOVE_STARTED" in skipped and "ARRIVED" in skipped


def test_history_chain_verifies_and_detects_tampering(rig):
    rig.go_online_remote()
    assert rig.host.move("NOWHERE") == HCACK_PARAMETER_INVALID
    assert rig.host.move("A") == HCACK_ACCEPTED_FINISH_LATER
    rig.advance(5.0)
    assert rig.host.wait_for_event(gem_ids.CEID_ARRIVED) is not None
    assert rig.equipment.wait_idle()

    records = rig.history.records()
    kinds = {record["kind"] for record in records}
    assert {"host_command", "host_command_reply", "control_state", "robot_state", "event", "event_ack"} <= kinds
    assert [r["data"]["hcack"] for r in records if r["kind"] == "host_command_reply"] == [3, 4]
    assert [r["data"]["to"] for r in records if r["kind"] == "control_state"] == ["HOST_OFFLINE", "ONLINE_REMOTE"]
    assert rig.history.verify() is True

    # 手動把「HCACK=3」那筆改成 4
    path = rig.history.path
    lines = path.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if json.loads(line)["kind"] == "host_command_reply")
    tampered = json.loads(lines[index])
    tampered["data"]["hcack"] = 4
    lines[index] = json.dumps(tampered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    assert rig.history.verify() is False


def test_alarm_is_recorded_in_history(make_rig):
    rig = make_rig(battery_initial=25.0)
    rig.go_online_remote()
    rig.host.move("A")
    rig.advance(3.0)
    assert rig.host.wait_for_alarm(gem_ids.ALID_LOW_BATTERY, is_set=True) is not None
    rig.equipment.charge_robot()
    assert rig.host.wait_for_alarm(gem_ids.ALID_LOW_BATTERY, is_set=False) is not None
    assert rig.equipment.wait_idle()

    alarms = [r["data"] for r in rig.history.records() if r["kind"] == "alarm"]
    assert [(a["name"], a["set"], a["reported"]) for a in alarms] == [
        ("LOW_BATTERY", True, True),
        ("LOW_BATTERY", False, True),
    ]
    assert rig.history.verify() is True
    assert all(isinstance(item, (HostEvent, HostAlarm)) for item in rig.host.received)
