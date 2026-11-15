-- 稼動率與統計的 view（SQLite 3.25+，用到視窗函式與遞迴 CTE）。
-- E10 風格，非標準實作：狀態對應見 state_map，是作者自己的簡化選擇。
-- 全部由「狀態轉換的時間差」計算，不用取樣。

DROP VIEW IF EXISTS v_daily_availability;
DROP VIEW IF EXISTS v_daily_state_seconds;
DROP VIEW IF EXISTS v_state_intervals;
DROP VIEW IF EXISTS v_daily_alarm_stats;
DROP VIEW IF EXISTS v_alarm_intervals;
DROP VIEW IF EXISTS v_move_durations;

-- 狀態區間 [start, end)。
-- 時間軸上的「點」有兩種，依 seq 排序後用 LEAD() 取下一個點的時間當作區間終點：
--   1. state_changes 的每一筆（轉換或心跳）：從這一刻起狀態是 to_state。
--   2. seq 缺號：從缺口前最後一筆訊息的時間起狀態是 UNKNOWN，
--      一直到缺口之後第一筆 state 訊息（最慢 60 秒內會有心跳）才重新知道狀態。
-- 最後一個點的區間終點 = 該設備收到的最後一筆訊息的時間（不外推到「現在」）。
CREATE VIEW v_state_intervals AS
WITH points AS (
    SELECT equipment_id, seq, ts_us, to_state AS raw_state
    FROM state_changes
    UNION ALL
    SELECT equipment_id, seq_from, ts_from_us, 'UNKNOWN'
    FROM gaps
    WHERE source = 'seq_missing'
),
ordered AS (
    SELECT equipment_id,
           seq,
           raw_state,
           ts_us AS start_us,
           LEAD(ts_us) OVER (PARTITION BY equipment_id ORDER BY seq) AS next_us
    FROM points
)
SELECT o.equipment_id,
       o.seq,
       o.raw_state,
       m.e10_state,
       o.start_us,
       COALESCE(o.next_us, s.last_ts_us) AS end_us,
       (COALESCE(o.next_us, s.last_ts_us) - o.start_us) / 1000000.0 AS seconds
FROM ordered o
JOIN state_map m ON m.raw_state = o.raw_state
JOIN ingest_state s ON s.equipment_id = o.equipment_id
WHERE COALESCE(o.next_us, s.last_ts_us) > o.start_us;

-- 每日（UTC）各 E10 狀態的秒數。跨午夜的區間會被切開。
CREATE VIEW v_daily_state_seconds AS
WITH RECURSIVE bounds AS (
    SELECT equipment_id,
           MIN(start_us) / 86400000000 AS first_day,
           (MAX(end_us) - 1) / 86400000000 AS last_day
    FROM v_state_intervals
    GROUP BY equipment_id
),
days (equipment_id, day_no, last_day) AS (
    SELECT equipment_id, first_day, last_day FROM bounds
    UNION ALL
    SELECT equipment_id, day_no + 1, last_day FROM days WHERE day_no < last_day
)
SELECT d.equipment_id,
       date(d.day_no * 86400, 'unixepoch') AS day,
       i.e10_state,
       SUM(MIN(i.end_us, (d.day_no + 1) * 86400000000) - MAX(i.start_us, d.day_no * 86400000000)) / 1000000.0 AS seconds
FROM days d
JOIN v_state_intervals i
  ON i.equipment_id = d.equipment_id
 AND i.start_us < (d.day_no + 1) * 86400000000
 AND i.end_us > d.day_no * 86400000000
GROUP BY d.equipment_id, d.day_no, i.e10_state;

-- 每日稼動率 = PRODUCTIVE ÷（總時間 − OFFLINE − UNKNOWN）。
-- UNKNOWN（斷線或缺號的時段）不進分母，單獨一欄列出。分母為 0 時 availability 是 NULL。
CREATE VIEW v_daily_availability AS
SELECT equipment_id,
       day,
       productive_s,
       standby_s,
       unscheduled_down_s,
       offline_s,
       unknown_s,
       total_s,
       total_s - offline_s - unknown_s AS denominator_s,
       productive_s / NULLIF(total_s - offline_s - unknown_s, 0) AS availability
FROM (
    SELECT equipment_id,
           day,
           SUM(CASE WHEN e10_state = 'PRODUCTIVE' THEN seconds ELSE 0.0 END) AS productive_s,
           SUM(CASE WHEN e10_state = 'STANDBY' THEN seconds ELSE 0.0 END) AS standby_s,
           SUM(CASE WHEN e10_state = 'UNSCHEDULED_DOWN' THEN seconds ELSE 0.0 END) AS unscheduled_down_s,
           SUM(CASE WHEN e10_state = 'OFFLINE' THEN seconds ELSE 0.0 END) AS offline_s,
           SUM(CASE WHEN e10_state = 'UNKNOWN' THEN seconds ELSE 0.0 END) AS unknown_s,
           SUM(seconds) AS total_s
    FROM v_daily_state_seconds
    GROUP BY equipment_id, day
);

-- 每次警報：set 之後同一警報的下一筆若是 clear，就是解除時間。
-- 還沒解除，或中間有訊息遺失以致配不起來時，clear_ts 與 duration_s 是 NULL。
CREATE VIEW v_alarm_intervals AS
SELECT equipment_id,
       alarm,
       seq AS set_seq,
       ts AS set_ts,
       ts_us AS set_us,
       CASE WHEN next_action = 'clear' THEN next_ts END AS clear_ts,
       CASE WHEN next_action = 'clear' THEN (next_us - ts_us) / 1000000.0 END AS duration_s,
       text
FROM (
    SELECT a.*,
           LEAD(action) OVER w AS next_action,
           LEAD(ts) OVER w AS next_ts,
           LEAD(ts_us) OVER w AS next_us
    FROM alarms a
    WINDOW w AS (PARTITION BY equipment_id, alarm ORDER BY seq)
)
WHERE action = 'set';

-- 每日警報統計（依成立時間的 UTC 日期）。
CREATE VIEW v_daily_alarm_stats AS
SELECT equipment_id,
       substr(set_ts, 1, 10) AS day,
       alarm,
       COUNT(*) AS set_count,
       COUNT(clear_ts) AS cleared_count,
       SUM(duration_s) AS total_duration_s,
       AVG(duration_s) AS avg_duration_s,
       MAX(duration_s) AS max_duration_s
FROM v_alarm_intervals
GROUP BY equipment_id, substr(set_ts, 1, 10), alarm;

-- MOVE 任務耗時：MOVE_STARTED 之後的下一個事件若是同一站的 ARRIVED 才算完成。
-- 被警報中止、或兩者之間有 seq 缺號（配對不可信）的，arrived_ts 與 duration_s 是 NULL。
CREATE VIEW v_move_durations AS
SELECT e.equipment_id,
       e.seq AS start_seq,
       e.station,
       e.ts AS started_ts,
       e.ts_us AS started_us,
       CASE WHEN e.completed THEN e.next_ts END AS arrived_ts,
       CASE WHEN e.completed THEN (e.next_us - e.ts_us) / 1000000.0 END AS duration_s
FROM (
    SELECT m.*,
           m.next_event = 'ARRIVED'
           AND m.next_station = m.station
           AND NOT EXISTS (
               SELECT 1
               FROM gaps g
               WHERE g.equipment_id = m.equipment_id
                 AND g.source = 'seq_missing'
                 AND g.seq_from BETWEEN m.seq AND m.next_seq
           ) AS completed
    FROM (
        SELECT equipment_id, seq, ts, ts_us, event, station,
               LEAD(event) OVER w AS next_event,
               LEAD(station) OVER w AS next_station,
               LEAD(seq) OVER w AS next_seq,
               LEAD(ts) OVER w AS next_ts,
               LEAD(ts_us) OVER w AS next_us
        FROM events
        WHERE event IN ('MOVE_STARTED', 'ARRIVED')
        WINDOW w AS (PARTITION BY equipment_id ORDER BY seq)
    ) m
    WHERE m.event = 'MOVE_STARTED'
) e;
