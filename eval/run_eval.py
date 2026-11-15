"""執行評測：agent 透過 MCP（stdio）查唯讀 server，答案與資料庫算出的標準答案做確定性比對。

    python -m eval.run_eval --agent oracle                 # 腳本化 oracle，應該滿分
    python -m eval.run_eval --agent dont_know              # 永遠回不知道，只在「沒有答案」類得分
    python -m eval.run_eval --agent model                  # 真實模型；要先設 GEM_EVAL_MODEL，否則拒絕執行

沒給 --db 時會先用 simulate_day（--adversarial，固定劇本）在 --out 目錄產生資料。
每次執行的軌跡（工具呼叫、參數、回傳大小、耗時，以及模型的 token 與成本）寫進 --out/trajectory.jsonl，
格式是 history.py 的只追加雜湊鏈。評分只做確定性比對，沒有 LLM 評審。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp import Client, StdioServerParameters

from eval.agents import (
    REAL_MODEL_MIN_REPEATS,
    Agent,
    AgentAnswer,
    DontKnowAgent,
    OracleAgent,
    real_agent_from_env,
)
from eval.ground_truth import NO_DATA, GroundTruth
from history import History

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.yaml"
DEFAULT_TOLERANCE = 1e-6


def load_questions(path: str | Path = QUESTIONS_PATH) -> list[dict[str, Any]]:
    questions = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    ids = [question["id"] for question in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("question ids must be unique")
    for question in questions:
        if question["category"] not in ("answerable", "no_answer"):
            raise ValueError(f"{question['id']}: unknown category")
        if "expected" in question or "answer" in question:
            raise ValueError(f"{question['id']}: answers must come from ground_truth.py, not from the question file")
    return questions


# ---- 評分：確定性比對 -------------------------------------------------------------------


def is_correct(question: dict[str, Any], expected: Any, answer: AgentAnswer) -> bool:
    if answer.error is not None:
        return False
    if expected is NO_DATA:
        return answer.no_data
    if answer.no_data:
        return False
    value = answer.value
    answer_type = question.get("answer_type")
    if answer_type == "bool":
        return isinstance(value, bool) and value == expected
    if answer_type == "int":
        return isinstance(value, int) and not isinstance(value, bool) and value == expected
    if answer_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return abs(float(value) - float(expected)) <= float(question.get("tolerance", DEFAULT_TOLERANCE))
    if answer_type == "str":
        return isinstance(value, str) and value.strip() == str(expected)
    raise ValueError(f"{question['id']}: unknown answer_type {answer_type!r}")


def _jsonable(value: Any) -> Any:
    return "NO_DATA" if value is NO_DATA else value


# ---- 軌跡 -------------------------------------------------------------------------------


class McpToolCaller:
    """agent 唯一能用的工具通道：MCP client + 把每次呼叫寫進雜湊鏈。"""

    def __init__(self, client: Client, trajectory: History, run_id: str, agent_name: str) -> None:
        self._client = client
        self._trajectory = trajectory
        self._base = {"run_id": run_id, "agent": agent_name}
        self._context: dict[str, Any] = {}
        self.calls = 0

    def set_context(self, question_id: str, repeat: int) -> None:
        self._context = {"question_id": question_id, "repeat": repeat}

    def record(self, kind: str, data: dict[str, Any]) -> None:
        self._trajectory.append(kind, {**self._base, **self._context, **data})

    async def list_tools(self) -> list[dict[str, Any]]:
        listing = await self._client.list_tools()
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in listing.tools
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        is_error = False
        try:
            result = await self._client.call_tool(name, arguments)
            text = "".join(getattr(block, "text", "") for block in result.content)
            if result.is_error or not isinstance(result.structured_content, dict):
                is_error = True
                structured = {"ok": False, "error": {"code": "TOOL_CALL_REJECTED", "message": text[:300]}}
            else:
                structured = result.structured_content
        except Exception as exc:  # noqa: BLE001 - 傳輸層的錯也要留在軌跡裡
            is_error, text = True, ""
            structured = {"ok": False, "error": {"code": "MCP_ERROR", "message": type(exc).__name__}}
        self.calls += 1
        self.record(
            "tool_call",
            {
                "tool": name,
                "arguments": arguments,
                "is_error": is_error,
                "ok": structured.get("ok"),
                "no_data": structured.get("no_data"),
                "truncated": structured.get("truncated"),
                "response_chars": len(text),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )
        return structured


# ---- 執行 -------------------------------------------------------------------------------


@dataclass
class QuestionResult:
    question_id: str
    category: str
    repeat: int
    status: str  # answered / not_run
    correct: bool = False
    expected: Any = None
    answer: Any = None
    no_data: bool = False
    error: str | None = None
    tool_calls: int = 0
    cost_usd: float = 0.0


@dataclass
class EvalReport:
    agent: str
    run_id: str
    repeats: int
    results: list[QuestionResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    budget_usd: float | None = None
    stopped_reason: str | None = None
    trajectory_path: str = ""
    trajectory_ok: bool = False

    def _count(self, category: str | None = None) -> tuple[int, int]:
        rows = [r for r in self.results if category is None or r.category == category]
        return sum(r.correct for r in rows), len(rows)

    @property
    def correct(self) -> int:
        return self._count()[0]

    @property
    def total(self) -> int:
        return self._count()[1]

    def summary(self) -> dict[str, Any]:
        answerable, no_answer = self._count("answerable"), self._count("no_answer")
        return {
            "agent": self.agent,
            "run_id": self.run_id,
            "repeats": self.repeats,
            "score": f"{self.correct}/{self.total}",
            "answerable": f"{answerable[0]}/{answerable[1]}",
            "no_answer": f"{no_answer[0]}/{no_answer[1]}",
            "not_run": sum(r.status == "not_run" for r in self.results),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "budget_usd": self.budget_usd,
            "stopped_reason": self.stopped_reason,
            "trajectory": self.trajectory_path,
            "trajectory_chain_ok": self.trajectory_ok,
            "wrong": [r.question_id for r in self.results if r.status == "answered" and not r.correct],
        }


def stdio_server(db_path: str | Path, history_path: str | Path | None) -> StdioServerParameters:
    args = [str(ROOT / "mcp_server.py"), "--db", str(db_path)]
    if history_path:
        args += ["--history", str(history_path)]
    return StdioServerParameters(command=sys.executable, args=args, cwd=str(ROOT))


async def run_eval(  # noqa: PLR0913
    agent: Agent,
    *,
    db_path: str | Path,
    history_path: str | Path | None,
    trajectory_path: str | Path,
    questions: list[dict[str, Any]] | None = None,
    repeats: int = 1,
    budget_usd: float | None = None,
    server: Any = None,
) -> EvalReport:
    """跑一次評測。``server`` 預設是 stdio 子行程；測試可以傳 MCPServer 物件走 in-process。"""
    questions = load_questions() if questions is None else questions
    truth = GroundTruth(db_path, history_path)
    expected = {question["id"]: truth.answer(question["truth"]) for question in questions}

    trajectory = History(trajectory_path)
    report = EvalReport(
        agent=agent.name,
        run_id=uuid.uuid4().hex,
        repeats=repeats,
        budget_usd=budget_usd,
        trajectory_path=str(trajectory_path),
    )
    base = {"run_id": report.run_id, "agent": agent.name}
    trajectory.append("run_start", {**base, "questions": len(questions), "repeats": repeats, "budget_usd": budget_usd})

    async with Client(server if server is not None else stdio_server(db_path, history_path)) as client:
        tools = McpToolCaller(client, trajectory, report.run_id, agent.name)
        for repeat in range(repeats):
            for question in questions:
                result = QuestionResult(question["id"], question["category"], repeat, "not_run")
                result.expected = _jsonable(expected[question["id"]])
                report.results.append(result)
                if report.stopped_reason is not None:
                    continue
                if budget_usd is not None and report.total_cost_usd >= budget_usd:
                    report.stopped_reason = "budget_exceeded"
                    trajectory.append("budget_stop", {**base, "spent_usd": round(report.total_cost_usd, 6), "budget_usd": budget_usd})
                    continue

                tools.set_context(question["id"], repeat)
                calls_before, started = tools.calls, time.perf_counter()
                try:
                    answer = await agent.answer(question, tools)
                except Exception as exc:  # noqa: BLE001 - agent 出錯算答錯，評測繼續
                    answer = AgentAnswer(error=f"agent raised {type(exc).__name__}")
                result.status = "answered"
                result.correct = is_correct(question, expected[question["id"]], answer)
                result.answer, result.no_data, result.error = answer.value, answer.no_data, answer.error
                result.tool_calls = tools.calls - calls_before
                result.cost_usd = answer.usage.cost_usd
                report.total_cost_usd += answer.usage.cost_usd
                tools.record(
                    "answer",
                    {
                        "category": question["category"],
                        "expected": result.expected,
                        "answer": answer.value,
                        "no_data": answer.no_data,
                        "error": answer.error,
                        "correct": result.correct,
                        "tool_calls": result.tool_calls,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                        "input_tokens": answer.usage.input_tokens,
                        "output_tokens": answer.usage.output_tokens,
                        "cost_usd": round(answer.usage.cost_usd, 6),
                    },
                )

    trajectory.append("run_end", {**base, **{k: v for k, v in report.summary().items() if k not in ("trajectory", "trajectory_chain_ok")}})
    report.trajectory_ok = trajectory.verify()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", choices=("oracle", "dont_know", "model"), default="oracle")
    parser.add_argument("--db", type=Path, help="既有的資料庫；不給就在 --out 產生模擬資料")
    parser.add_argument("--history", type=Path, help="設備歷史 JSONL（搭配 --db）")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "out")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--details", action="store_true", help="同時印出每一題的結果")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    budget = None
    repeats = args.repeats
    if args.agent == "oracle":
        agent: Agent = OracleAgent()
    elif args.agent == "dont_know":
        agent = DontKnowAgent()
    else:
        enabled = real_agent_from_env()
        if enabled is None:
            print("真實模型 client 預設關閉：請先設定環境變數 GEM_EVAL_MODEL（例如 claude-opus-5-5）。", file=sys.stderr)
            return 2
        agent, budget = enabled
        repeats = max(REAL_MODEL_MIN_REPEATS, repeats)  # 每題至少重跑 3 次

    db_path, history_path = args.db, args.history
    if db_path is None:
        from simulate_day import run_simulation  # noqa: PLC0415

        db_path = args.out / "sim.sqlite"
        history_path = args.out / "sim.history.jsonl"
        if not db_path.exists():
            print(f"產生模擬資料：{db_path}", file=sys.stderr)
            run_simulation(db_path, hours=24, adversarial=True, history_path=history_path)

    report = asyncio.run(
        run_eval(
            agent,
            db_path=db_path,
            history_path=history_path,
            trajectory_path=args.out / "trajectory.jsonl",
            repeats=repeats,
            budget_usd=budget,
        ),
    )
    print(json.dumps(report.summary(), ensure_ascii=False, indent=2))
    if args.details:
        for result in report.results:
            print(json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
