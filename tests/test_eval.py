"""評測骨架：oracle 滿分、「永遠不知道」只在無答案題得分、軌跡鏈驗證通過、成本上限會停。"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import mcp_server
from eval import agents, ground_truth, run_eval
from eval.agents import AgentAnswer, DontKnowAgent, OracleAgent, Usage
from eval.ground_truth import NO_DATA, GroundTruth
from history import verify_file
from ingest import connect as connect_readwrite


def run(agent, sim_data, tmp_path, **kwargs):
    kwargs.setdefault("trajectory_path", tmp_path / "trajectory.jsonl")
    return asyncio.run(run_eval.run_eval(agent, db_path=sim_data.db, history_path=sim_data.history, **kwargs))


def records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---- 題目檔 -------------------------------------------------------------------------------


def test_question_file_shape():
    questions = run_eval.load_questions()
    assert 18 <= len(questions) <= 30
    assert sum(q["category"] == "no_answer" for q in questions) >= 5
    for question in questions:
        assert not {"expected", "answer", "answers"} & set(question)  # 答案不手寫在題目檔
        assert question["truth"]["kind"]
        assert question["oracle"]["tool"] in mcp_server.DESCRIPTIONS
        if question["category"] == "answerable":
            assert question["answer_type"] in ("int", "float", "bool", "str")


# ---- 標準答案由資料庫計算 --------------------------------------------------------------------


def test_ground_truth_is_computed_from_the_database(sim_data, tmp_path):
    truth = GroundTruth(sim_data.db, sim_data.history)
    day1 = {"equipment_id": "ROBOT01", "since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"}
    db = sqlite3.connect(str(sim_data.db))
    try:
        direct = db.execute("SELECT COUNT(*) FROM alarms WHERE action = 'set' AND ts < '2026-01-02'").fetchone()[0]
        view = db.execute("SELECT availability FROM v_daily_availability WHERE day = '2026-01-01'").fetchone()[0]
    finally:
        db.close()
    assert truth.answer({"kind": "alarm_count", **day1}) == direct == 2
    assert truth.answer({"kind": "alarm_total_duration_s", **day1}) == 1200.0
    assert truth.answer({"kind": "daily_value", "equipment_id": "ROBOT01", "day": "2026-01-01", "column": "availability"}) == view
    assert truth.answer({"kind": "current_state", "equipment_id": "ROBOT01"}) == "OFFLINE"
    assert truth.answer({"kind": "history_valid"}) is True

    # 換一個（空的）資料庫，同一題的答案就跟著變：不是寫死的
    connect_readwrite(tmp_path / "empty.sqlite").close()
    empty = GroundTruth(tmp_path / "empty.sqlite", None)
    assert empty.answer({"kind": "alarm_count", **day1}) is NO_DATA
    assert empty.answer({"kind": "current_state", "equipment_id": "ROBOT01"}) is NO_DATA
    assert empty.answer({"kind": "history_valid"}) is NO_DATA


def test_every_question_has_the_expected_kind_of_truth(sim_data):
    truth = GroundTruth(sim_data.db, sim_data.history)
    for question in run_eval.load_questions():
        answer = truth.answer(question["truth"])
        if question["category"] == "no_answer":
            assert answer is NO_DATA, question["id"]
        else:
            assert answer is not NO_DATA and answer is not None, question["id"]
            expected_type = {"int": int, "float": (int, float), "bool": bool, "str": str}[question["answer_type"]]
            assert isinstance(answer, expected_type), question["id"]


# ---- 評分 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer_type", "expected", "answer", "correct"),
    [
        ("int", 2, AgentAnswer(value=2), True),
        ("int", 2, AgentAnswer(value=3), False),
        ("int", 2, AgentAnswer(value=2.0), False),
        ("int", 1, AgentAnswer(value=True), False),
        ("int", 0, AgentAnswer(no_data=True), False),  # 0 不等於無資料
        ("float", 0.3853, AgentAnswer(value=0.38530000004), True),
        ("float", 0.3853, AgentAnswer(value=0.39), False),
        ("float", 0.5, AgentAnswer(value="0.5"), False),
        ("bool", False, AgentAnswer(value=False), True),
        ("bool", False, AgentAnswer(value=0), False),
        ("str", "OFFLINE", AgentAnswer(value=" OFFLINE "), True),
        ("str", "OFFLINE", AgentAnswer(value="offline"), False),
        ("int", NO_DATA, AgentAnswer(no_data=True), True),
        ("int", NO_DATA, AgentAnswer(value=0), False),  # 無資料不能答 0
        ("int", 2, AgentAnswer(value=2, error="unparseable"), False),
    ],
)
def test_scoring_is_a_deterministic_comparison(answer_type, expected, answer, correct):
    question = {"id": "q", "answer_type": answer_type}
    assert run_eval.is_correct(question, expected, answer) is correct


# ---- 兩個假 client ------------------------------------------------------------------------


def test_oracle_gets_full_marks_over_stdio_and_trajectory_chain_verifies(sim_data, tmp_path):
    report = run(OracleAgent(), sim_data, tmp_path)  # 預設走 stdio 子行程
    questions = run_eval.load_questions()

    assert report.correct == report.total == len(questions), report.summary()
    assert report.summary()["wrong"] == [] and report.stopped_reason is None
    assert report.trajectory_ok is True and verify_file(tmp_path / "trajectory.jsonl") is True

    rows = records(tmp_path / "trajectory.jsonl")
    assert [rows[0]["kind"], rows[-1]["kind"]] == ["run_start", "run_end"]
    calls = [row["data"] for row in rows if row["kind"] == "tool_call"]
    answers = [row["data"] for row in rows if row["kind"] == "answer"]
    assert len(calls) == len(answers) == len(questions)
    for call in calls:  # 工具呼叫、參數、回傳大小、耗時
        assert call["run_id"] == report.run_id and call["agent"] == "oracle"
        assert call["tool"] in mcp_server.DESCRIPTIONS and isinstance(call["arguments"], dict)
        assert call["response_chars"] > 0 and call["duration_ms"] >= 0 and call["is_error"] is False
        assert call["question_id"] and call["repeat"] == 0
    assert all(answer["correct"] for answer in answers)
    assert {answer["question_id"] for answer in answers if answer["no_data"]} == {
        q["id"] for q in questions if q["category"] == "no_answer"
    }


def test_dont_know_client_scores_only_on_no_answer_questions(sim_data, tmp_path):
    report = run(DontKnowAgent(), sim_data, tmp_path, server=mcp_server.build_server(sim_data.db, sim_data.history))
    questions = {q["id"]: q for q in run_eval.load_questions()}
    no_answer = {qid for qid, q in questions.items() if q["category"] == "no_answer"}

    assert {r.question_id for r in report.results if r.correct} == no_answer
    assert report.correct == len(no_answer) < report.total
    summary = report.summary()
    assert summary["no_answer"] == f"{len(no_answer)}/{len(no_answer)}"
    assert summary["answerable"] == f"0/{len(questions) - len(no_answer)}"
    assert report.trajectory_ok is True
    assert not any(row["kind"] == "tool_call" for row in records(tmp_path / "trajectory.jsonl"))


def test_runs_append_to_one_trajectory_chain(sim_data, tmp_path):
    server = lambda: mcp_server.build_server(sim_data.db, sim_data.history)  # noqa: E731
    first = run(OracleAgent(), sim_data, tmp_path, server=server())
    second = run(DontKnowAgent(), sim_data, tmp_path, server=server())
    assert first.run_id != second.run_id
    rows = records(tmp_path / "trajectory.jsonl")
    assert [row["seq"] for row in rows] == list(range(len(rows)))
    assert {row["data"]["run_id"] for row in rows} == {first.run_id, second.run_id}
    assert verify_file(tmp_path / "trajectory.jsonl") is True


# ---- 真實模型 client：預設關閉；重跑與成本上限的機制用假的計價 client 驗證 ------------------------


def test_real_model_client_is_disabled_unless_the_env_var_is_set():
    assert agents.real_agent_from_env({}) is None
    assert agents.real_agent_from_env({"ANTHROPIC_API_KEY": "x"}) is None  # 有金鑰也不會自動啟用
    assert agents.real_agent_from_env({"GEM_EVAL_MODEL": "  "}) is None
    with pytest.raises(ValueError, match="no price known"):
        agents.real_agent_from_env({"GEM_EVAL_MODEL": "some-unknown-model"})
    assert agents.REAL_MODEL_MIN_REPEATS >= 3 and agents.DEFAULT_BUDGET_USD > 0


class PricedOracle(OracleAgent):
    """oracle + 每題假裝花 0.40 美元，用來驗證成本上限。"""

    name = "priced_oracle"

    async def answer(self, question, tools):
        answer = await super().answer(question, tools)
        answer.usage = Usage(input_tokens=1000, output_tokens=100, cost_usd=0.40, model_calls=1)
        return answer


def test_cost_cap_stops_the_run_and_reports_it(sim_data, tmp_path):
    questions = run_eval.load_questions()[:4]
    report = run(
        PricedOracle(),
        sim_data,
        tmp_path,
        questions=questions,
        repeats=3,
        budget_usd=1.0,
        server=mcp_server.build_server(sim_data.db, sim_data.history),
    )
    assert report.total == 12  # 4 題 x 3 次
    answered = [r for r in report.results if r.status == "answered"]
    assert len(answered) == 3  # 0.4 x 3 = 1.2 >= 1.0 -> 第 4 次起不再執行
    assert report.total_cost_usd == pytest.approx(1.2)
    assert report.stopped_reason == "budget_exceeded"
    assert report.summary()["not_run"] == 9 and report.correct == 3
    kinds = [row["kind"] for row in records(tmp_path / "trajectory.jsonl")]
    assert kinds.count("budget_stop") == 1 and kinds.count("answer") == 3
    answer_rows = [row["data"] for row in records(tmp_path / "trajectory.jsonl") if row["kind"] == "answer"]
    assert all(row["cost_usd"] == 0.4 and row["input_tokens"] == 1000 for row in answer_rows)
    assert report.trajectory_ok is True


def test_repeats_run_every_question_that_many_times(sim_data, tmp_path):
    questions = run_eval.load_questions()[:3]
    report = run(OracleAgent(), sim_data, tmp_path, questions=questions, repeats=3,
                 server=mcp_server.build_server(sim_data.db, sim_data.history))
    assert report.correct == report.total == 9
    assert sorted({r.repeat for r in report.results}) == [0, 1, 2]


def test_model_answer_parsing_is_deterministic():
    parse = agents.parse_final_answer
    assert parse('thinking...\n{"no_data": false, "answer": 2}').value == 2
    assert parse('{"no_data": true, "answer": null}').no_data is True
    assert parse('{"no_data": false, "answer": "OFFLINE"}').value == "OFFLINE"
    assert parse("I think the answer is 2").error is not None
    assert parse('{"no_data": false, "answer": null}').error is not None
    assert parse("").error is not None


def test_ground_truth_module_does_not_use_the_server():
    source = (run_eval.ROOT / "eval" / "ground_truth.py").read_text(encoding="utf-8")
    assert "mcp_server" not in source and "import mcp" not in source
    assert ground_truth.NO_DATA is NO_DATA
