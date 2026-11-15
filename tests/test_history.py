from __future__ import annotations

import json

import pytest

from history import GENESIS_HASH, History, verify_file


def fill(path, count=5):
    history = History(path)
    for index in range(count):
        history.append("test", {"index": index, "note": f"第 {index} 筆"})
    return history


def rewrite(path, mutate):
    lines = path.read_text(encoding="utf-8").splitlines()
    mutate(lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def test_chain_links_each_record_to_the_previous(tmp_path):
    history = fill(tmp_path / "h.jsonl")
    records = history.records()
    assert [record["seq"] for record in records] == [0, 1, 2, 3, 4]
    assert records[0]["prev"] == GENESIS_HASH
    for previous, current in zip(records, records[1:]):
        assert current["prev"] == previous["hash"]
    assert history.verify() is True


def test_empty_history_verifies(tmp_path):
    assert History(tmp_path / "h.jsonl").verify() is True


@pytest.mark.parametrize("target", [0, 2, 4])
def test_editing_any_record_breaks_verify(tmp_path, target):
    path = tmp_path / "h.jsonl"
    history = fill(path)

    def mutate(lines):
        record = json.loads(lines[target])
        record["data"]["index"] = 999
        lines[target] = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    rewrite(path, mutate)
    assert history.verify() is False
    assert verify_file(path) is False


def test_recomputing_one_hash_still_breaks_the_chain(tmp_path):
    """竄改者就算把被改那筆的 hash 重算，下一筆的 prev 也對不上。"""
    import hashlib

    path = tmp_path / "h.jsonl"
    history = fill(path)

    def mutate(lines):
        record = json.loads(lines[1])
        record["data"]["index"] = 999
        del record["hash"]
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        record["hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    rewrite(path, mutate)
    assert history.verify() is False


def test_deleting_or_reordering_lines_breaks_verify(tmp_path):
    path = tmp_path / "h.jsonl"
    history = fill(path)
    rewrite(path, lambda lines: lines.pop(2))
    assert history.verify() is False

    path2 = tmp_path / "h2.jsonl"
    history2 = fill(path2)

    def swap(lines):
        lines[1], lines[2] = lines[2], lines[1]

    rewrite(path2, swap)
    assert history2.verify() is False


def test_reopen_continues_the_chain(tmp_path):
    path = tmp_path / "h.jsonl"
    fill(path, 3)
    reopened = History(path)
    reopened.append("test", {"index": 3})
    assert [record["seq"] for record in reopened.records()] == [0, 1, 2, 3]
    assert reopened.verify() is True


def test_refuses_to_append_to_a_tampered_file(tmp_path):
    path = tmp_path / "h.jsonl"
    fill(path, 3)
    rewrite(path, lambda lines: lines.__setitem__(1, lines[1].replace("第 1 筆", "被改過")))
    with pytest.raises(ValueError):
        History(path)
