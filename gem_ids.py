"""設備與測試主機共用的 ID 常數（本專案自行指定的數值，不是任何標準定義的）。"""

from __future__ import annotations

# Remote command
RCMD_MOVE = "MOVE"
CPNAME_STATION = "STATION"

# Collection events
CEID_MOVE_STARTED = 1001
CEID_ARRIVED = 1002
CEID_LOW_BATTERY_SET = 1003  # secsgem 的 Alarm 需要 set/clear 各一個 CEID
CEID_LOW_BATTERY_CLEARED = 1004

CEID_NAMES = {
    CEID_MOVE_STARTED: "MOVE_STARTED",
    CEID_ARRIVED: "ARRIVED",
    CEID_LOW_BATTERY_SET: "LOW_BATTERY_SET",
    CEID_LOW_BATTERY_CLEARED: "LOW_BATTERY_CLEARED",
}

# Data values
DVID_STATION = 2001

# Alarms（secsgem 0.3.0 的 S5F1 ALID 只接受數值型別，所以 LOW_BATTERY 是名稱、1 是 ALID）
ALID_LOW_BATTERY = 1
ALID_NAMES = {ALID_LOW_BATTERY: "LOW_BATTERY"}
ALCD_SET_BIT = 0x80  # ALCD bit 8：1=成立，0=解除

# 預設報告 ID（測試主機用 S2F33 定義）
RPTID_STATION = 1
