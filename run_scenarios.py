"""把 scenarios.yaml 的每個情境分別對 gate.decide 與 MCP 工具（request_move）跑一遍，輸出混淆表。

    python run_scenarios.py [--audit scenarios_audit.jsonl]

* gate 路徑：直接組出請求、快照、歷史，呼叫純函式。
* mcp 路徑：SDK 的 MCP client -> mcp_guardian_server 的工具 -> Guardian -> 確定性的假主機端（SimHostPort）。
  假主機端模擬設備的 HCACK 檢查並記下每一次真的送出的 MOVE；這條路徑不經過 HSMS
  （真的 HSMS 執行路徑、競態、斷線在 pytest 裡另外測）。

expected 與實際不一致時，逐條列出並以非 0 結束；不會、也不應該去改 expected。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from mcp import Client

import gate
from gate import RecentRequest
from guardian import Guardian, SimHostPort
from history import History
from mcp_guardian_server import build_server
from policy import Policy, load_policy
from request_store import RequestStore

ROOT = Path(__file__).resolve().parent
SCENARIOS_PATH = ROOT / "scenarios.yaml"
NOW = 1_000_000.0  # 情境用的固定時鐘
TRIP_COST = 10.0
TYPES = ("benign", "invalid", "state", "abuse", "escalate", "adversarial")


def load_scenarios(path: str | Path = SCENARIOS_PATH) -> list[dict[str, Any]]:
    scenarios = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    ids = [scenario["id"] for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario ids must be unique")
    for scenario in scenarios:
        if scenario["type"] not in TYPES:
            raise ValueError(f"{scenario['id']}: unknown type")
        gate.Verdict(scenario["expected"]["verdict"])
        gate.Reason(scenario["expected"]["reason"])
    return scenarios


def _station(request: dict[str, Any]) -> Any:
    if "station_repeat" in request:
        return request["station_repeat"]["text"] * request["station_repeat"]["times"]
    return request.get("station")


def _request_id(scenario: dict[str, Any]) -> str:
    return scenario["request"].get("request_id", "scn-" + scenario["id"].replace("_", "-"))[:64]


@dataclass
class Outcome:
    scenario_id: str
    kind: str
    path: str  # gate / mcp
    expected: tuple[str, str]
    actual: tuple[str, str]
    sent: int | None = None
    expected_sent: int | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def run_gate(scenario: dict[str, Any], policy: Policy) -> Outcome:
    setup, request = scenario.get("setup") or {}, scenario["request"]
    equipment_id = request.get("equipment_id", "ROBOT01")
    snapshot = None
    if setup.get("host_up", True):
        snapshot = {
            "equipment_id": equipment_id,
            "communicating": True,
            "control_state": setup.get("control_state", "ONLINE_REMOTE"),
            "robot_state": setup.get("robot_state", "IDLE"),
            "battery": setup.get("battery", 100),
            "trip_cost": TRIP_COST,
            "taken_at": NOW,
        }
    recent = [
        RecentRequest(f"prior-{index}", "ROBOT01", item["station"], NOW - item["seconds_ago"], item["verdict"])
        for index, item in enumerate(setup.get("history") or [])
    ]
    decision = gate.decide(
        {
            "equipment_id": equipment_id,
            "rcmd": request.get("rcmd", "MOVE"),
            "station": _station(request),
            "request_id": _request_id(scenario),
        },
        snapshot,
        policy,
        NOW,
        recent,
    )
    return _judge(scenario, "gate", (decision.verdict.value, decision.reason.value), None)


async def run_mcp(scenario: dict[str, Any], policy: Policy, audit: History, workdir: Path) -> Outcome:
    setup, request = scenario.get("setup") or {}, scenario["request"]
    port = SimHostPort()
    port.up = setup.get("host_up", True)
    port.control_state = setup.get("control_state", "ONLINE_REMOTE")
    port.robot_state = setup.get("robot_state", "IDLE")
    port.battery = setup.get("battery", 100)
    store = RequestStore(workdir / f"{scenario['id']}.sqlite")
    for index, item in enumerate(setup.get("history") or []):
        store.insert(
            {
                "request_id": f"prior-{index}",
                "equipment_id": "ROBOT01",
                "station": item["station"],
                "created_at": NOW - item["seconds_ago"],
                "verdict": item["verdict"],
                "reason": "OK",
                "status": "COMPLETED" if item["verdict"] == "EXECUTE" else "REJECTED",
                "policy_sha256": policy.sha256,
                "result": {},
            },
        )
    seeded = store.count()
    guardian = Guardian(policy, port, audit, store, trip_cost=TRIP_COST, clock=lambda: NOW)
    arguments = {
        "equipment_id": request.get("equipment_id", "ROBOT01"),
        "station": _station(request),
        "request_id": _request_id(scenario),
    }
    results = []
    async with Client(build_server(guardian)) as client:
        for _ in range(scenario.get("repeat", 1)):
            result = await client.call_tool("request_move", arguments)
            results.append(result.structured_content if not result.is_error else None)
    outcome = None
    last = results[-1]
    actual = (last["verdict"], last["reason"]) if isinstance(last, dict) and "verdict" in last else ("ERROR", "TOOL_ERROR")
    outcome = _judge(scenario, "mcp", actual, len(port.sent))
    if any(result != {**results[0], "replayed": True} for result in results[1:] if results[0] is not None):
        outcome.problems.append("a resend of the same request_id did not return the first result")
    pending = [r["request_id"] for r in store.with_status("PENDING_APPROVAL")]
    if actual[0] != "ESCALATE" and pending:
        outcome.problems.append(f"left something in the approval queue: {pending}")
    if actual[0] == "REJECT" and store.count() - seeded > 1:
        outcome.problems.append("a rejected request left more than its own record behind")
    store.close()
    return outcome


def _judge(scenario: dict[str, Any], path: str, actual: tuple[str, str], sent: int | None) -> Outcome:
    expected = (scenario["expected"]["verdict"], scenario["expected"]["reason"])
    outcome = Outcome(scenario["id"], scenario["type"], path, expected, actual, sent, scenario.get("expect_sent", 0))
    if actual != expected:
        outcome.problems.append(f"expected {expected[0]}/{expected[1]}, got {actual[0]}/{actual[1]}")
    if sent is not None:
        if actual[0] != "EXECUTE" and sent:
            outcome.problems.append(f"a move was sent {sent} time(s) although the verdict was {actual[0]}")
        if sent != outcome.expected_sent:
            outcome.problems.append(f"expected {outcome.expected_sent} S2F41 to be sent, {sent} were sent")
    return outcome


def run_all(audit_path: str | Path, workdir: str | Path | None = None) -> list[Outcome]:
    policy = load_policy()
    scenarios = load_scenarios()
    audit = History(audit_path)
    outcomes = [run_gate(s, policy) for s in scenarios if "gate" in s.get("via", ["gate", "mcp"])]

    async def mcp_all(folder: Path) -> list[Outcome]:
        return [await run_mcp(s, policy, audit, folder) for s in scenarios if "mcp" in s.get("via", ["gate", "mcp"])]

    if workdir is None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            outcomes += asyncio.run(mcp_all(Path(folder)))
    else:
        outcomes += asyncio.run(mcp_all(Path(workdir)))
    return outcomes


def confusion_table(outcomes: list[Outcome], path: str) -> str:
    rows = [o for o in outcomes if o.path == path]
    labels = sorted({o.expected for o in rows} | {o.actual for o in rows})
    counts = Counter((o.expected, o.actual) for o in rows)
    name = lambda label: f"{label[0]}/{label[1]}"  # noqa: E731
    width = max(len(name(label)) for label in labels)
    lines = [f"[{path}] rows = expected, columns = actual ({len(rows)} scenarios)"]
    lines.append(" " * width + "  " + " ".join(f"{index:>3}" for index in range(len(labels))))
    for index, expected in enumerate(labels):
        cells = " ".join(f"{counts.get((expected, actual), 0) or '.':>3}" for actual in labels)
        lines.append(f"{name(expected):<{width}}  {cells}   <- column {index}")
    return "\n".join(lines)


def summary_by_type(outcomes: list[Outcome]) -> str:
    lines = ["type          path  scenarios  match"]
    for kind in TYPES:
        for path in ("gate", "mcp"):
            rows = [o for o in outcomes if o.kind == kind and o.path == path]
            if rows:
                lines.append(f"{kind:<12}  {path:<4}  {len(rows):>9}  {sum(o.ok for o in rows):>5}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audit", type=Path, default=Path("scenarios_audit.jsonl"))
    args = parser.parse_args()
    args.audit.unlink(missing_ok=True)

    outcomes = run_all(args.audit)
    print(confusion_table(outcomes, "gate"), end="\n\n")
    print(confusion_table(outcomes, "mcp"), end="\n\n")
    print(summary_by_type(outcomes), end="\n\n")
    failures = [o for o in outcomes if not o.ok]
    for outcome in failures:
        print(f"MISMATCH {outcome.scenario_id} [{outcome.path}]: " + "; ".join(outcome.problems))
    audit_ok = History(args.audit).verify()
    print(f"{len(outcomes) - len(failures)}/{len(outcomes)} runs match expected; audit chain verify() = {audit_ok}")
    return 0 if not failures and audit_ok else 1


if __name__ == "__main__":
    sys.exit(main())
