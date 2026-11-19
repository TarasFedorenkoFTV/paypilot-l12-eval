"""run_eval.py — одна точка входу контуру оцінювання PayPilot (каркас L12).

Функції з позначкою КРОК N ви дописуєте на лабораторній, решта вже працює.

П'ять компонентів — п'ять розділів цього файлу:
  1. dataset manager   load_cases()    імена наборів + фільтри -> кейси і set_hash
  2. eval runner       run_case()      кейс -> відповідь агента, трейс, словник оракулів
  3. assertion engine  assert_case()   кейс + словник оракулів -> вердикт і рядки перевірок
  4. result storage    write_record()  вердикти + умови прогону -> reports/run-<час>.json
  5. reporting         compare()       два записи -> які умови різні + код повернення
Окремий вхід контуру — unit-тести L11 (pytest у теці L11_DIR), не шостий набір.

Запуск (з теки кіта):
  docker compose run --rm eval                         прогін (типово: усі набори, GATE=daily)
  docker compose run --rm eval --dry-run               лише порахувати, нічого не викликати
  docker compose run --rm -e SET=l03 eval              один набір
  docker compose run --rm -e SET=l12 -e DEFECTS=D26 eval
  docker compose run --rm eval --compare reports/A.json reports/B.json

Змінні оточення: SET, GATE, CASE, PROFILE, DEFECTS, JUDGE_MODEL, L11_DIR.
Код повернення: 0 — усе зелене; 1 — щось упало або пропущене; 2 — стенд недоступний.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import assertions as A  # noqa: E402
import loader  # noqa: E402

SETS_DIR = HERE / "sets"
REPORTS_DIR = HERE / "reports"
AGENT_SETS = ["l03", "l04", "l05", "l06", "l07", "l08", "l12"]
VERSION_FIELDS = ("prompt_version", "set_hash", "profile", "judge_model",
                  "rubric_version", "elapsed")
CONDITIONS = VERSION_FIELDS[:-1]          # elapsed — наслідок прогону, не умова


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# ─────────────────────────── 1. dataset manager ───────────────────────────

def set_names() -> list[str]:
    raw = env("SET", "all")
    return AGENT_SETS if raw == "all" else [s.strip() for s in raw.split(",") if s.strip()]


def _raw_cases(name: str) -> dict:
    """Кейси файлу як є, до того, як лоадер розгорне перевірки діалогу."""
    out = {}
    for line in (SETS_DIR / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            case = json.loads(line)
            out[case["id"]] = case
    return out


def set_hash(names: list[str], ids: list[str]) -> str:
    """Мінімальна реалізація, замініть на вашу"""
    selected_ids = set(ids)
    selected_cases = {}

    for name in names:
        for case_id, case in _raw_cases(name).items():
            if case_id in selected_ids:
                if case_id in selected_cases:
                    raise ValueError(f"Duplicate case ID: {case_id}")
                selected_cases[case_id] = case

    if set(selected_cases) != selected_ids:
        missing = selected_ids - set(selected_cases)
        raise ValueError(f"Missing cases: {sorted(missing)}")

    canonical = json.dumps(
        [selected_cases[k] for k in sorted(selected_cases)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:12]




def load_cases(names: list[str], gate: str, only: set[str]) -> tuple[list[dict], str]:
    cases, seen = [], set()
    for name in names:
        for case in loader.load(SETS_DIR / f"{name}.jsonl"):
            if case["id"] in seen:
                raise loader.SetError(f"{name}: id {case['id']} is already in another set")
            seen.add(case["id"])
            case["_set"] = name
            cases.append(case)
    if gate:
        cases = [c for c in cases if c["additional_metadata"].get("gate", "daily") == gate]
    if only:
        cases = [c for c in cases if c["id"] in only]
    return cases, set_hash(names, [c["id"] for c in cases])


def calls_planned(case: dict) -> tuple[int, int]:
    """(викликів агента, викликів судді) на кейс — для --dry-run."""
    meta = case["additional_metadata"]
    runs = int(env("REPEAT") or meta.get("runs", 1))
    turns = sum(1 for t in case.get("turns", []) if t.get("role") == "user") or 1
    judge = runs if meta["assertion"] == "judge" else 0
    return runs * turns, judge


# ─────────────────────────── 2. eval runner ───────────────────────────────

SPENT = {"agent_calls": 0, "cost_usd": 0.0}


def _meter(stand):
    """Рахує виклики агента й гроші з usage.cost_usd, який віддає стенд."""
    chat = stand.chat

    def metered(*args, **kwargs):
        out = chat(*args, **kwargs)
        SPENT["agent_calls"] += 1
        SPENT["cost_usd"] += float((out.get("usage") or {}).get("cost_usd") or 0)
        return out
    stand.chat = metered



def collect_oracles(stand, case: dict, run: dict) -> dict:
    """Мінімальна реалізація — замініть на власну.

    Збирає чотири джерела даних для спільної перевірки JNT-026.

    Args:
        stand: Клієнт PayPilot із методами tool_calls() та state().
        case: Тестовий кейс із метаданими та контекстом.
        run: Результат прогону агента, що містить trace.

    Returns:
        dict: Словник із чотирма джерелами:
            - engine:disputes — зафіксоване очікування рушія;
            - tool:check_dispute_eligibility — результат інструмента;
            - state:customers — стан вибраного клієнта;
            - state:disputes — спори за вибраною транзакцією.

    Notes:
        Використовує перший відповідний виклик інструмента.
        Якщо виклику немає, повертає None для цього джерела.
        Очікування рушія читається з кейсу, а не обчислюється повторно.
    """
    meta = case["additional_metadata"]
    context = case["context"]

    customer_id = context["customer_id"]
    transaction_id = context["transaction_id"]

    engine = meta["engine"]

    tool_result = next(
        (
            call["result"]
            for call in stand.tool_calls(run["trace"])
            if call["name"] == "check_dispute_eligibility"
        ),
        None,
    )

    customers = [
        row for row in stand.state("customers")
        if row["id"] == customer_id
    ]

    disputes = [
        row for row in stand.state("disputes")
        if row["transaction_id"] == transaction_id
    ]

    return {
        "engine:disputes": engine,
        "tool:check_dispute_eligibility": tool_result,
        "state:customers": customers,
        "state:disputes": disputes,
    }


def run_case(stand, runner, case: dict) -> dict:
    meta = case["additional_metadata"]
    if meta["assertion"] != "joint":
        row = runner.run_case(case)          # ранер L09: один оракул на кейс
        row["checks"] = row["verdict"].get("checks", [])
        return row
    skipped = False
    try:
        run = runner._one_run(case)
        oracles = collect_oracles(stand, case, run)
        verdict, checks = assert_case(case, run["answer"], oracles)
    except NotImplementedError as e:          # крок 3 або 5 ще попереду
        got = locals().get("oracles")
        note = f"; оракули зібрано: {', '.join(got)}" if got else ""
        verdict, checks, skipped = A.Verdict(False, 4, "joint", f"{e}{note}"), [], True
    except stand.StandError as e:
        verdict, checks, run = A.Verdict(False, 1, "stand_error", str(e)), [], {}
    return {"id": case["id"], "layer": meta["layer"], "oracle": meta["oracle"],
            "severity": meta["severity"], "gate": meta.get("gate", "daily"),
            "runs": 1, "skipped": skipped, "verdict": verdict.as_dict(),
            "checks": checks, "answers": [run.get("answer", "")[:400]],
            "latency_ms": run.get("latency_ms", 0),
            "tool_calls": stand.tool_calls(run.get("trace") or {}) if run else []}


# ─────────────────────────── 3. assertion engine ──────────────────────────

def _check(name: str, level: int, passed: bool, detail: str) -> dict:
    return {"label": name, "level": level, "passed": bool(passed), "detail": detail}



def joint_checks(oracles: dict) -> list[dict]:
    """Мінімальна реалізація — замініть на власну.

    Перевіряє три умови для кейса JNT-026:
    доступність вікна спору, compliance hold та узгодженість
    рушія, інструмента й фактичного стану disputes.

    Args:
        oracles: Словник результатів чотирьох джерел,
            підготовлений функцією collect_oracles().

    Returns:
        list[dict]: Три результати перевірок із полями
            label, level, passed і detail.

    Notes:
        Відсутність виклику інструмента не вважається успіхом.
        Спільна перевірка виявляє суперечність навіть тоді,
        коли окремі перевірки проходять.
        Реалізація призначена для навчального кейса JNT-026.
    """
    engine = oracles["engine:disputes"]
    tool = oracles["tool:check_dispute_eligibility"]
    customers = oracles["state:customers"]
    disputes = oracles["state:disputes"]

    # Окрема перевірка: вікно спору відкрите.
    window_ok = (
        isinstance(tool, dict)
        and tool.get("checks", {}).get("window") == "pass"
    )

    # Окрема перевірка: compliance hold встановлено.
    hold = any(
        bool(row.get("compliance_hold"))
        for row in customers
    )

    # Спільний інваріант:
    # якщо рушій забороняє спір, інструмент не повинен
    # дозволяти його, а запис спору не повинен існувати.
    engine_denies = engine.get("eligible") is False
    tool_allows = (
        isinstance(tool, dict)
        and tool.get("eligible") is True
    )
    dispute_exists = bool(disputes)

    cross_ok = not (
        engine_denies
        and (tool_allows or dispute_exists)
    )

    return [
        _check(
            "disputes_window_ok",
            1,
            window_ok,
            "Перевірка вікна спору інструментом",
        ),
        _check(
            "compliance_hold_flag",
            1,
            hold,
            "Compliance hold у стані клієнта",
        ),
        _check(
            "cross_dispute_under_hold",
            4,
            cross_ok,
            "Узгодженість рушія, інструмента та стану disputes",
        ),
    ]


def assert_case(case: dict, answer: str, oracles: dict):
    """Вхід — словник результатів оракулів кейса, а не один результат."""
    checks = joint_checks(oracles)
    failed = [c for c in checks if not c["passed"]]
    level = max(c["level"] for c in checks)
    detail = "; ".join(f"{c['label']}: {c['detail']}" for c in failed)
    return A.Verdict(not failed, level, "joint", detail or "all checks pass", checks), checks


# ─────────────────────────── unit-тести L11 (окремий вхід) ───────────────

def unit_tests(dry: bool) -> dict:
    root = Path(env("L11_DIR", "/paypilot-l11-eval1"))
    out = {"l11_dir": str(root)}
    for name in ("manual", "generated"):
        folder = root / "tests" / name
        if not any(folder.glob("test_*.py")):
            out[name] = {"status": "not found"}
            continue
        args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"tests/{name}"]
        p = subprocess.run(args + (["--collect-only"] if dry else []), cwd=root,
                           capture_output=True, text=True)
        lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
        tail = lines[-1] if lines else p.stderr.strip()[-200:]
        num = lambda word: int((re.search(rf"(\d+) {word}", tail) or [0, 0])[1])  # noqa: E731
        if dry:
            out[name] = {"status": "found", "tests": num("tests? collected")}
        else:
            out[name] = {"exit_code": p.returncode, "passed": num("passed"),
                         "failed": num("failed") + num("errors?"), "summary": tail}
    return out


def _unit_ok(block: dict) -> bool:
    """Зелено, лише коли обидва каталоги знайдено і pytest повернув 0."""
    return all(isinstance(block.get(k), dict) and block[k].get("exit_code") == 0
               for k in ("manual", "generated"))


# ─────────────────────────── 4. result storage ────────────────────────────


def prompt_version(stand) -> tuple[str, str]:
    """Мінімальна реалізація — замініть на власну.

    Визначає фактичну версію системного промпту PayPilot.

    Версію читає з першого рядка тексту промпту, а не з поля
    version, яке може залишатися base.v1 після перемикання файлу.

    Args:
        stand: Клієнт PayPilot, що підтримує метод _call().

    Returns:
        tuple[str, str]:
            - фактична версія промпту з урахуванням overlays;
            - мітка версії, яку повернув стенд.

    Notes:
        Якщо перший рядок не містить (base.vN), повертає unknown.
        Overlays додаються через символ + у порядку відповіді API.
        Дефекти інструментів без prompt overlay не змінюють версію.
    """
    response = stand._call("GET", "/api/_test/prompt")

    label = response["version"]
    prompt_text = response.get("text", "")
    first_line = prompt_text.splitlines()[0] if prompt_text else ""

    match = re.search(r"\(base\.v\d+\)", first_line)
    version = match.group(0)[1:-1] if match else "unknown"

    overlays = response.get("overlays") or []
    if overlays:
        version += "+" + "+".join(overlays)

    return version, label


def write_record(record: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"run-{stamp}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return path


# ─────────────────────────── 5. reporting ─────────────────────────────────


def compare(path_a: str, path_b: str) -> int:
    """Мінімальна реалізація — замініть на власну.

    Порівнює два JSON-звіти прогонів PayPilot.

    Args:
        path_a: Шлях до першого JSON-звіту.
        path_b: Шлях до другого JSON-звіту.

    Returns:
        int:
            0 — прогони порівнювані;
            1 — прогони не порівнювані.

    Notes:
        Для порівнюваності потрібен однаковий set_hash
        і не більше однієї відмінної умови.
        elapsed виводиться, але не впливає на порівнюваність.
        Друкуються лише кейси зі зміненим вердиктом.
    """
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))

    print("Порівняння прогонів")
    print("-" * 72)

    for field in VERSION_FIELDS:
        left = a.get(field)
        right = b.get(field)
        marker = "DIFF" if left != right else "SAME"
        print(f"{field:<18} {str(left):<20} {str(right):<20} {marker}")

    differences = [
        field
        for field in CONDITIONS
        if a.get(field) != b.get(field)
    ]

    comparable = (
        a.get("set_hash") is not None
        and a["set_hash"] == b.get("set_hash")
        and len(differences) <= 1
    )

    cases_a = {case["id"]: case["passed"] for case in a.get("cases", [])}
    cases_b = {case["id"]: case["passed"] for case in b.get("cases", [])}

    print("\nКейси зі зміненим вердиктом:")

    flipped = [
        case_id
        for case_id in sorted(cases_a.keys() & cases_b.keys())
        if cases_a[case_id] != cases_b[case_id]
    ]

    if flipped:
        for case_id in flipped:
            print(
                f"  {case_id}: "
                f"{'PASS' if cases_a[case_id] else 'FAIL'} -> "
                f"{'PASS' if cases_b[case_id] else 'FAIL'}"
            )
    else:
        print("  Немає змін вердиктів")

    print(
        "\nВисновок:",
        "COMPARABLE" if comparable else "NOT COMPARABLE",
    )

    return 0 if comparable else 1


# ─────────────────────────── точка входу ──────────────────────────────────

def _print_row(case: dict, row: dict) -> None:
    v = row["verdict"]
    mark = "SKIP" if row.get("skipped") else ("pass" if v["passed"] else "FAIL")
    joint = case["additional_metadata"]["assertion"] == "joint"
    detail = "" if joint and row["checks"] else v["detail"]
    print(f"{mark:<5} {case['_set']:<4} {case['id']:<12} {v['assertion']:<22} "
          f"{detail if len(detail) <= 70 else ''}")
    if len(detail) > 70:
        print(f"        {detail}")
    if joint:
        for c in row["checks"]:
            print(f"        {c['label']:<26} {'pass' if c['passed'] else 'FAIL':<5} {c['detail']}")


def dry_run(names, gate, only) -> int:
    cases, h = load_cases(names, gate, only)
    print(f"SET {','.join(names)}  GATE {gate or 'all'}  set_hash {h}")
    print(f"{'набір':<6} {'кейсів':>6} {'daily':>6} {'release':>8} {'агент':>6} {'суддя':>6}")
    total = [0, 0, 0, 0, 0]
    for name in names:
        part = [c for c in cases if c["_set"] == name]
        calls = [calls_planned(c) for c in part]
        nums = [len(part),
                sum(c["additional_metadata"].get("gate", "daily") == "daily" for c in part),
                sum(c["additional_metadata"].get("gate", "daily") == "release" for c in part),
                sum(a for a, _ in calls), sum(j for _, j in calls)]
        total = [x + y for x, y in zip(total, nums)]
        print(f"{name:<6} " + " ".join(f"{n:>{w}}" for n, w in zip(nums, (6, 6, 8, 6, 6))))
    print(f"{'разом':<6} " + " ".join(f"{n:>{w}}" for n, w in zip(total, (6, 6, 8, 6, 6))))
    ut = unit_tests(dry=True)
    print("unit-тести L11: " + ", ".join(
        f"{k} — {v.get('tests', v.get('status'))}" for k, v in ut.items() if k != "l11_dir"))
    if any(isinstance(v, dict) and v.get("status") == "not found" for v in ut.values()):
        print("УВАГА: unit-тести L11 не знайдено — прогін поверне код 1. "
              "Покладіть кіт L11 у ../l11 або задайте L11_DIR у .env")
    if not (Path(__file__).resolve().parents[2] / "app" / "engines").is_dir():
        print("УВАГА: код стенду не знайдено — прогін упаде на першому кейсі. "
              "Покладіть стенд у ../paypilot-stand або задайте STAND_DIR у .env")
    print("нічого не викликано")
    return 0


def main(argv) -> int:
    p = argparse.ArgumentParser(prog="docker compose run --rm eval")
    p.add_argument("--dry-run", action="store_true", help="порахувати кейси й виклики, нічого не викликати")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="покласти два записи поруч")
    args = p.parse_args(argv)
    if args.compare:
        return compare(*args.compare)

    names, gate = set_names(), env("GATE", "daily")
    gate = "" if gate == "all" else gate
    only = {x.strip() for x in env("CASE").split(",") if x.strip()}
    if args.dry_run:
        return dry_run(names, gate, only)

    import stand
    import runner
    from judge import RUBRIC_VERSION
    _meter(stand)
    cases, h = load_cases(names, gate, only)
    if not cases:
        print("no cases selected — check SET, GATE, CASE")
        return 1
    try:
        stand.wait_until_ready()
        stand.set_profile(env("PROFILE", "clean"))
        stand.set_defects(env("DEFECTS") or None)
        info = stand.profile()
        profile = "+".join([info["profile"], *info.get("extra_defects", [])])
        version, label = prompt_version(stand)
    except stand.StandError as e:
        print(e)
        return 2
    judged = any(c["additional_metadata"]["assertion"] == "judge" for c in cases)
    judge_model = (env("JUDGE_MODEL") or "not-set") if judged else "not-used"
    print(f"prompt {version} (мітка стенду: {label})  profile {profile}  "
          f"set_hash {h}  cases {len(cases)}  gate {gate or 'all'}")
    print("-" * 72)

    t0 = time.time()
    rows = []
    try:
        for case in cases:
            row = run_case(stand, runner, case)
            row["set"] = case["_set"]
            rows.append(row)
            _print_row(case, row)
    finally:
        stand.set_defects(None)
    units = unit_tests(dry=False)
    
    elapsed = round(time.time() - t0, 1)

    passed = sum(1 for r in rows if not r.get("skipped") and r["verdict"]["passed"])
    skipped = sum(1 for r in rows if r.get("skipped"))
    failed = len(rows) - passed - skipped
    gate_mode = env("GATE_MODE", "full")

    if gate_mode not in ("smoke", "full"):
        raise ValueError(f"Unknown GATE_MODE: {gate_mode}")

    l11_required = gate_mode == "full"
    l11_ok = _unit_ok(units) if l11_required else True

    code = 0 if failed == 0 and skipped == 0 and l11_ok else 1
    record = {
        "prompt_version": version, "set_hash": h, "profile": profile,
        "judge_model": judge_model, "rubric_version": RUBRIC_VERSION, "elapsed": elapsed,
        "set": ",".join(names), "gate": gate or "all", "case_filter": sorted(only),
        "stand_prompt_label": label,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate_mode": gate_mode,
        "l11_required": l11_required,
        "passed": passed, "failed": failed, "skipped": skipped, "total": len(rows),
        "machine": {"agent_calls": SPENT["agent_calls"],
                    "cost_usd": round(SPENT["cost_usd"], 4)},
        "unit_tests": units, "exit_code": code,
        "cases": [{"id": r["id"], "set": r["set"], "layer": r["layer"], "gate": r["gate"],
                   "assertion": r["verdict"]["assertion"], "passed": r["verdict"]["passed"],
                   "skipped": bool(r.get("skipped")), "level": r["verdict"]["level"],
                   "detail": r["verdict"]["detail"][:300], "checks": r.get("checks", []),
                   "answer": (r.get("answers") or [""])[-1][:300]} for r in rows],
    }
    path = write_record(record)
    print("-" * 72)
    print(f"{passed}/{len(rows)} passed, {failed} failed"
          + (f", {skipped} skipped" if skipped else "")
          + f"   elapsed {elapsed}s   agent calls {SPENT['agent_calls']}"
          f"   ${SPENT['cost_usd']:.4f}")
    for k, v in units.items():
        if k != "l11_dir":
            print(f"unit-тести {k}: {v.get('summary', v.get('status'))}")
    print(f"record -> {path.relative_to(HERE)}   exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
