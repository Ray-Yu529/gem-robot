-- 資料表（SQLite 方言；這台機器沒有 docker，TimescaleDB 版本沒有做）。
-- 時間一律 UTC：ts 是 ISO 8601 文字（...Z），ts_us 是同一時刻自 1970-01-01 起的整數微秒，
-- 時間差都用 ts_us 算，避免浮點誤差。顯示時才轉時區。

-- 每台設備每個 seq 只認一次（跨四種訊息），重複投遞靠這張表擋掉。
CREATE TABLE IF NOT EXISTS message_log (
    equipment_id TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    type         TEXT    NOT NULL CHECK (type IN ('state', 'event', 'alarm', 'telemetry')),
    PRIMARY KEY (equipment_id, seq)
);

CREATE TABLE IF NOT EXISTS state_changes (
    equipment_id TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    site         TEXT    NOT NULL,
    ts           TEXT    NOT NULL,
    ts_us        INTEGER NOT NULL,
    from_state   TEXT,                 -- 第一筆為 NULL
    to_state     TEXT    NOT NULL,
    heartbeat    INTEGER NOT NULL CHECK (heartbeat IN (0, 1)),  -- 1 = 每 60 秒補發的目前狀態
    PRIMARY KEY (equipment_id, seq)
);

CREATE TABLE IF NOT EXISTS events (
    equipment_id TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    site         TEXT    NOT NULL,
    ts           TEXT    NOT NULL,
    ts_us        INTEGER NOT NULL,
    event        TEXT    NOT NULL,     -- MOVE_STARTED / ARRIVED / GAP
    station      TEXT,                 -- MOVE_STARTED / ARRIVED 才有
    details      TEXT,                 -- GAP 的其餘欄位（JSON）
    PRIMARY KEY (equipment_id, seq)
);

CREATE TABLE IF NOT EXISTS alarms (
    equipment_id TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    site         TEXT    NOT NULL,
    ts           TEXT    NOT NULL,
    ts_us        INTEGER NOT NULL,
    alarm        TEXT    NOT NULL,
    action       TEXT    NOT NULL CHECK (action IN ('set', 'clear')),
    text         TEXT,                 -- 選用的自由文字；不可信的資料，原樣保存
    PRIMARY KEY (equipment_id, seq)
);

CREATE TABLE IF NOT EXISTS telemetry (
    equipment_id TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    site         TEXT    NOT NULL,
    ts           TEXT    NOT NULL,
    ts_us        INTEGER NOT NULL,
    battery      REAL    NOT NULL,
    PRIMARY KEY (equipment_id, seq)
);

-- schema 不合的訊息：不丟掉，連原因一起留著。同一則（topic + payload）只留一筆。
CREATE TABLE IF NOT EXISTS rejected (
    id             INTEGER PRIMARY KEY,
    payload_sha256 TEXT NOT NULL UNIQUE,
    topic          TEXT NOT NULL,
    payload        TEXT NOT NULL,
    reason         TEXT NOT NULL,
    received_at    TEXT NOT NULL
);

-- 資料缺口。
--   source = 'seq_missing'        ingest 看到 seq 跳號。ts_from = 缺口前最後一筆的時間，ts_to = 缺口後第一筆的時間。
--   source = 'publisher_reported' publisher 恢復後自己回報（event = GAP）。ts 是被丟掉的第一筆／最後一筆的時間。
-- 稼動率的 UNKNOWN 只用 seq_missing（任何遺失都會造成跳號；publisher_reported 留作對帳）。
CREATE TABLE IF NOT EXISTS gaps (
    equipment_id TEXT    NOT NULL,
    seq_from     INTEGER NOT NULL,     -- 第一個缺的 seq
    seq_to       INTEGER NOT NULL,     -- 最後一個缺的 seq
    missing      INTEGER NOT NULL,
    ts_from      TEXT    NOT NULL,
    ts_from_us   INTEGER NOT NULL,
    ts_to        TEXT    NOT NULL,
    ts_to_us     INTEGER NOT NULL,
    source       TEXT    NOT NULL CHECK (source IN ('seq_missing', 'publisher_reported')),
    PRIMARY KEY (equipment_id, seq_from, source)
);

-- 每台設備目前收到的最大 seq 與它的時間（缺號偵測的依據，也是時間軸的終點）。
CREATE TABLE IF NOT EXISTS ingest_state (
    equipment_id TEXT    PRIMARY KEY,
    last_seq     INTEGER NOT NULL,
    last_ts      TEXT    NOT NULL,
    last_ts_us   INTEGER NOT NULL
);

-- 狀態對應：E10 風格的簡化對應，作者自己的選擇，不是 SEMI E10 的標準實作。
CREATE TABLE IF NOT EXISTS state_map (
    raw_state TEXT PRIMARY KEY,
    e10_state TEXT NOT NULL
);
INSERT OR IGNORE INTO state_map (raw_state, e10_state) VALUES
    ('MOVING',  'PRODUCTIVE'),
    ('IDLE',    'STANDBY'),
    ('ALARM',   'UNSCHEDULED_DOWN'),
    ('OFFLINE', 'OFFLINE'),
    ('UNKNOWN', 'UNKNOWN');

CREATE INDEX IF NOT EXISTS idx_state_changes_ts ON state_changes (equipment_id, ts_us);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry (equipment_id, ts_us);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (equipment_id, ts_us);
CREATE INDEX IF NOT EXISTS idx_alarms_ts ON alarms (equipment_id, ts_us);
