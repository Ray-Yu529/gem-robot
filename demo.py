"""Demo：同一個 process 內啟動設備與測試主機，下 MOVE -> A，印出完整訊息流。

    python demo.py [--move-seconds 2] [--sml] [--history demo_history.jsonl]

用真實時鐘（機器人真的「移動」幾秒）；全部在 127.0.0.1 上，不接任何真實設備。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import gem_ids
from fake_robot import FakeRobot, RobotConfig
from gem_equipment import RobotGemEquipment
from history import History
from host_client import HostClient, stop_pair
from message_tap import MessageTap, TappedMessage

_print_lock = threading.Lock()
_t0 = time.monotonic()


def say(text: str) -> None:
    with _print_lock:
        print(f"[{time.monotonic() - _t0:6.2f}s] {text}", flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--move-seconds", type=float, default=2.0, help="覆寫設定檔的移動秒數（預設 2）")
    parser.add_argument("--sml", action="store_true", help="同時印出每則訊息的 SML 內容")
    parser.add_argument("--history", type=Path, default=Path("demo_history.jsonl"))
    args = parser.parse_args()

    if args.history.exists():
        args.history.unlink()  # demo 每次從乾淨的歷史開始

    config = replace(RobotConfig.from_file(), move_seconds=args.move_seconds)
    robot = FakeRobot(config)  # 預設用 time.monotonic
    history = History(args.history)
    port = free_port()
    equipment = RobotGemEquipment(robot, history, port=port)
    host = HostClient(port=port)

    def show(message: TappedMessage) -> None:
        # 每則訊息只在「送出端」印一次
        if message.direction != "tx":
            return
        from_equipment = message.endpoint == equipment.settings.name
        arrow = "HOST <-- EQP " if from_equipment else "HOST --> EQP "
        suffix = " W" if message.wbit else ""
        text = f"{arrow} {message.label}{suffix}" if message.is_data else f"{arrow} (HSMS) {message.label}"
        if args.sml and message.is_data:
            text += "\n" + "\n".join("            " + line for line in message.sml.splitlines())
        say(text)

    tap = MessageTap(show).install()
    try:
        say(f"-- 啟動設備（passive）與主機（active），127.0.0.1:{port}")
        equipment.start()
        host.start()
        if not (host.wait_communicating(10) and equipment.waitfor_communicating(10)):
            say("!! 通訊建立失敗")
            return 1
        say(f"-- 通訊已建立；控制狀態 = {equipment.control_state_name}")

        say("-- 主機：OFF-LINE 時先試一次 MOVE（預期 HCACK=2）")
        say(f"   HCACK = {host.move('A')}")

        say("-- 主機：S1F17 要求 ON-LINE")
        say(f"   ONLACK = {host.go_online()}；控制狀態 = {equipment.control_state_name}")

        say("-- 主機：S2F33/S2F35/S2F37 定義、連結、啟用事件報告")
        say(f"   (DRACK, LRACK, ERACK) = {host.enable_move_events()}")

        say("-- 主機：S2F41 RCMD=MOVE STATION=A")
        say(f"   HCACK = {host.move('A')}（4 = 已接受，完成後以事件通知）")

        started = host.wait_for_event(gem_ids.CEID_MOVE_STARTED, timeout=5)
        say(f"   主機收到事件：{started.name if started else None} {started.values if started else ''}")

        say("-- 主機：移動中再下一次 MOVE（預期 HCACK=2）")
        say(f"   HCACK = {host.move('B')}")

        arrived = host.wait_for_event(gem_ids.CEID_ARRIVED, timeout=args.move_seconds + 5)
        say(f"   主機收到事件：{arrived.name if arrived else None} {arrived.values if arrived else ''}")
        say(f"-- 機器人：state={robot.state().value} position={robot.position()} battery={robot.battery():.1f}%")
        equipment.wait_idle()
        ok = started is not None and arrived is not None
    finally:
        stop_pair(equipment, host)
        tap.uninstall()

    say(f"-- 歷史：{len(history.records())} 筆，寫在 {args.history}；verify() = {history.verify()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
