"""Самоперевірка кроків L12 без стенду й без моделі.

    docker compose run --rm --entrypoint pytest eval tests -q
    docker compose run --rm --entrypoint pytest eval tests -q -k step2   # лише крок 2

До кроку N тести цього кроку червоні — так і має бути. Тест перевіряє
властивість, а не ваш код рядок за рядком: годиться будь-яка реалізація,
яка її має.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run_eval  # noqa: E402


# ── крок 2: set_hash ─────────────────────────────────────────────────────

@pytest.fixture
def sets_copy(tmp_path, monkeypatch):
    """Копія наборів, щоб тести могли її псувати."""
    folder = tmp_path / "sets"
    shutil.copytree(ROOT / "sets", folder)
    monkeypatch.setattr(run_eval, "SETS_DIR", folder)
    return folder


def _hash(names, gate="daily"):
    _, h = run_eval.load_cases(names, gate, set())
    return h


def _lines(path):
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_step2_hash_is_stable_and_12_chars(sets_copy):
    assert _hash(["l03"]) == _hash(["l03"])
    assert len(_hash(["l03"])) == 12


def test_step2_added_case_changes_hash(sets_copy):
    before = _hash(["l03"])
    path = sets_copy / "l03.jsonl"
    case = json.loads(_lines(path)[0])
    case["id"] = "TMP-999"
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")
    assert _hash(["l03"]) != before


def test_step2_edited_case_changes_hash(sets_copy):
    before = _hash(["l03"])
    path = sets_copy / "l03.jsonl"
    rows = [json.loads(x) for x in _lines(path)]
    rows[0]["input"] += " Thanks."
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8", newline="\n")
    assert _hash(["l03"]) != before


def test_step2_crlf_and_line_order_do_not_change_hash(sets_copy):
    before = _hash(["l03"])
    path = sets_copy / "l03.jsonl"
    path.write_text("\r\n".join(reversed(_lines(path))) + "\r\n",
                    encoding="utf-8", newline="")
    assert _hash(["l03"]) == before


def test_step2_key_order_does_not_change_hash(sets_copy):
    before = _hash(["l03"])
    path = sets_copy / "l03.jsonl"
    rows = [json.loads(x) for x in _lines(path)]
    path.write_text("".join(json.dumps(dict(reversed(list(r.items()))), ensure_ascii=False)
                            + "\n" for r in rows), encoding="utf-8", newline="\n")
    assert _hash(["l03"]) == before


def test_step2_hash_covers_every_file(sets_copy):
    before = _hash(["l03", "l04"])
    path = sets_copy / "l04.jsonl"
    path.write_text("\n".join(_lines(path)[:-1]) + "\n", encoding="utf-8", newline="\n")
    assert _hash(["l03", "l04"]) != before


# ── крок 3: словник оракулів ─────────────────────────────────────────────

class FakeStand:
    """Стенд без мережі: віддає те, що записали в тесті."""

    def __init__(self, hold, rows):
        self._hold, self._rows = hold, rows

    @staticmethod
    def tool_calls(tree):
        return tree.get("calls", [])

    def state(self, table):
        if table == "customers":
            return [{"id": "CUS-0001", "compliance_hold": 0},
                    {"id": "CUS-0006", "compliance_hold": int(self._hold)}]
        if table == "disputes":
            return ([{"transaction_id": "TX-0601"}] * self._rows
                    + [{"transaction_id": "TX-0101"}])
        return []


def _joint_case():
    return json.loads(_lines(ROOT / "sets" / "l12.jsonl")[0])


def _run(tool_eligible):
    tool = {"eligible": tool_eligible, "checks": {"window": "pass"}}
    return {"answer": "...", "trace": {"calls": [
        {"name": "check_dispute_eligibility", "result": tool}]}}


def _oracles(tool_eligible, hold, rows):
    return run_eval.collect_oracles(FakeStand(hold, rows), _joint_case(), _run(tool_eligible))


def test_step3_oracles_dict_has_four_sources():
    oracles = _oracles(True, hold=True, rows=1)
    assert set(oracles) >= {"engine:disputes", "tool:check_dispute_eligibility",
                            "state:customers", "state:disputes"}
    assert oracles["engine:disputes"]["eligible"] is False
    assert oracles["tool:check_dispute_eligibility"]["eligible"] is True
    assert [r["id"] for r in oracles["state:customers"]] == ["CUS-0006"]
    assert len(oracles["state:disputes"]) == 1          # лише TX-0601


# ── крок 4: версія промпту ───────────────────────────────────────────────

class PromptStand:
    def __init__(self, first_line, overlays=()):
        self.p = {"version": "base.v1", "overlays": list(overlays),
                  "text": first_line + "\n\n## 1. Role and tone\n..."}

    def _call(self, method, path):
        return self.p


def test_step4_version_comes_from_prompt_text():
    v, label = run_eval.prompt_version(PromptStand("# PayPilot — system prompt (base.v4)"))
    assert (v, label) == ("base.v4", "base.v1")


def test_step4_v1_stays_v1():
    v, _ = run_eval.prompt_version(PromptStand("# PayPilot — system prompt (base.v1)"))
    assert v == "base.v1"


def test_step4_overlays_are_appended():
    v, _ = run_eval.prompt_version(PromptStand("# PayPilot — system prompt (base.v1)", ["D06"]))
    assert v == "base.v1+D06"


# ── крок 5: спільна перевірка ────────────────────────────────────────────

def _checks(tool_eligible, hold, rows):
    rows_out = run_eval.joint_checks(_oracles(tool_eligible, hold, rows))
    return {c["label"]: c["passed"] for c in rows_out}


def test_step5_three_rows_with_expected_names():
    assert set(_checks(False, True, 0)) == {
        "disputes_window_ok", "compliance_hold_flag", "cross_dispute_under_hold"}


def test_step5_clean_all_green():
    # без D26: інструмент каже «не можна», спору немає
    assert _checks(False, True, 0) == {"disputes_window_ok": True,
                                       "compliance_hold_flag": True,
                                       "cross_dispute_under_hold": True}


def test_step5_d26_only_joint_is_red():
    # з D26: інструмент каже «можна», рядок спору записано
    assert _checks(True, True, 1) == {"disputes_window_ok": True,
                                      "compliance_hold_flag": True,
                                      "cross_dispute_under_hold": False}


def test_step5_joint_reads_the_pair_not_the_flag():
    # холд стоїть, але спору немає: спільна перевірка зелена
    assert _checks(True, True, 0)["cross_dispute_under_hold"] is True


# ── крок 6: два записи поруч ─────────────────────────────────────────────

BASE = {"prompt_version": "base.v1", "set_hash": "a1b2c3d4e5f6", "profile": "clean",
        "judge_model": "not-used", "rubric_version": "rubric.v1", "elapsed": 100.0,
        "passed": 2, "failed": 0,
        "cases": [{"id": "X-1", "passed": True}, {"id": "X-2", "passed": True}]}


def _write(tmp_path, name, **change):
    path = tmp_path / name
    path.write_text(json.dumps({**BASE, **change}), encoding="utf-8")
    return str(path)


def test_step6_one_condition_differs_is_comparable(tmp_path):
    a = _write(tmp_path, "a.json")
    b = _write(tmp_path, "b.json", prompt_version="base.v4", elapsed=130.0)
    assert run_eval.compare(a, b) == 0


def test_step6_set_hash_differs_is_not_comparable(tmp_path):
    a = _write(tmp_path, "a.json")
    b = _write(tmp_path, "b.json", set_hash="ffffffffffff")
    assert run_eval.compare(a, b) == 1


def test_step6_two_conditions_differ_is_not_comparable(tmp_path):
    a = _write(tmp_path, "a.json")
    b = _write(tmp_path, "b.json", prompt_version="base.v4", judge_model="other")
    assert run_eval.compare(a, b) == 1


def test_step6_flipped_cases_are_printed(tmp_path, capsys):
    a = _write(tmp_path, "a.json")
    b = _write(tmp_path, "b.json", prompt_version="base.v4", failed=1, passed=1,
               cases=[{"id": "X-1", "passed": True}, {"id": "X-2", "passed": False}])
    run_eval.compare(a, b)
    assert "X-2" in capsys.readouterr().out
