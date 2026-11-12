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
    """КРОК 2. Хеш вмісту кейсів, що пішли в прогін: 12 символів sha256.

    Вимоги (перевірите на кроці 2):
      - дописаний або виправлений кейс змінює хеш;
      - перезбереження файлу без змін, CRLF після клонування на Windows,
        інший порядок ключів чи рядків у файлі — не змінюють;
      - інший GATE чи CASE (інший перелік ids) — змінює: хеш описує саме
        ті кейси, що пішли в прогін, а не файли цілком;
      - у хеш не потрапляє нічого, крім кейсів: ні час, ні шлях, ні звіт.
    Підказка: _raw_cases(name) повертає кейси файлу як є; ids — id кейсів
    після фільтрів GATE і CASE."""
    raise NotImplementedError("КРОК 2: set_hash() ще не написано")



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
    """КРОК 3. Ранер збирає для assertion словник результатів оракулів кейса.

    Assertion потім лише читає цей словник: жодного HTTP усередині перевірки.
    Для кейса JNT-026 потрібні чотири ключі:
      "engine:disputes"                — очікуване рушія, записане в кейсі
                                         (additional_metadata.engine);
      "tool:check_dispute_eligibility" — результат інструмента з трейсу
                                         (stand.tool_calls(run["trace"]));
      "state:customers"                — рядок клієнта з context.customer_id
                                         (stand.state("customers"));
      "state:disputes"                 — рядки спорів з context.transaction_id
                                         (stand.state("disputes")).
    tool_calls() повертає список {"name", "arguments", "result"}; "result" —
    уже словник. Якщо агент викликав інструмент кілька разів, беріть перший
    виклик. Якщо не викликав, покладіть None: це теж
    результат, і спільна перевірка має його прочитати."""
    raise NotImplementedError("КРОК 3: collect_oracles() ще не написано")



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
    """КРОК 5. Дві окремі перевірки й одна спільна. Поверніть три рядки
    _check(назва, рівень, пройдено, пояснення):
      disputes_window_ok        рівень 1  інструмент каже: вікно спору відкрите
      compliance_hold_flag      рівень 1  у клієнта стоїть compliance_hold
      cross_dispute_under_hold  рівень 4  рушій каже «не можна», отже інструмент
                                          не має казати «можна», а в disputes
                                          не має бути рядка по транзакції
    Спільна перевірка читає кілька оракулів одразу; на профілі clean вона
    зелена, з DEFECTS=D26 — червона, а дві окремі лишаються зеленими.
    Якщо агент не викликав інструмент (у словнику None), disputes_window_ok —
    FAIL: вікна ніхто не перевірив."""
    raise NotImplementedError("КРОК 5: joint_checks() ще не написано")



def assert_case(case: dict, answer: str, oracles: dict):
    """Вхід — словник результатів оракулів кейса, а не один результат."""
    checks = joint_checks(oracles)
    failed = [c for c in checks if not c["passed"]]
    level = max(c["level"] for c in checks)
    detail = "; ".join(f"{c['label']}: {c['detail']}" for c in failed)
    return A.Verdict(not failed, level, "joint", detail or "all checks pass", checks), checks


# ─────────────────────────── unit-тести L11 (окремий вхід) ───────────────

def unit_tests(dry: bool) -> dict:
    root = Path(env("L11_DIR", "/course/l11"))
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
    """КРОК 4. Повертає (версія для звіту, мітка стенду).

    Мітку стенд віддає в GET /api/_test/prompt -> "version", але вона завжди
    base.v1: стенд читає файл base.v1.md, хоч би що в нього змонтували.
    Яка версія діє насправді, видно в першому рядку "text":
      "# PayPilot — system prompt (base.v4)".
    Якщо список "overlays" не порожній, допишіть його через «+», як стенд."""
    p = stand._call("GET", "/api/_test/prompt")
    return p["version"], p["version"]       # КРОК 4: перша половина — неправда



def write_record(record: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"run-{stamp}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return path


# ─────────────────────────── 5. reporting ─────────────────────────────────

def compare(path_a: str, path_b: str) -> int:
    """КРОК 6. Покладіть два записи поруч.

    Надрукуйте шість полів версії обох записів і позначте, які різні
    (elapsed — наслідок прогону, не умова). Надрукуйте кейси, що змінили
    вердикт (поле "cases": id і passed). Код повернення:
      0 — порівнювані: set_hash однаковий і різниться не більше однієї умови;
      1 — не порівнювані."""
    raise NotImplementedError("КРОК 6: compare() ще не написано")



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
    code = 0 if failed == 0 and skipped == 0 and _unit_ok(units) else 1
    record = {
        "prompt_version": version, "set_hash": h, "profile": profile,
        "judge_model": judge_model, "rubric_version": RUBRIC_VERSION, "elapsed": elapsed,
        "set": ",".join(names), "gate": gate or "all", "case_filter": sorted(only),
        "stand_prompt_label": label,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
