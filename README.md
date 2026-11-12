# Кіт лабораторної L12: один контур замість шести запусків

Порядок дій описано в темі L12. Цей файл пояснює, що де лежить і якими командами запускати.

Стенд не змінюється: потрібен той самий `~/paypilot/paypilot-stand`, що й на попередніх заняттях (коміт `74f1995`), піднятий зі справжнім провайдером моделі. У кіті лежить ранер останніх занять (L03–L09) — це «ваші скрипти», з яких ви збираєте контур, — набори L03–L08, калібрувальний набір L09 і каркас `run_eval.py`.

## Що в папці

| Файл | Що це | Крок |
| --- | --- | --- |
| `run_eval.py` | **точка входу контуру.** Каркас: п'ять розділів-компонентів; функції з позначкою `КРОК N` дописуєте ви | 2–6 |
| `tests/test_steps.py` | самоперевірка кроків без стенду й без моделі (19 тестів): тести кроку N зеленіють, коли крок зроблено | 2–6 |
| `sets/l03.jsonl` … `sets/l08.jsonl` | набори кейсів L03–L08 | 2–6 |
| `sets/l09_labels.jsonl` | калібрувальний набір судді L09 (у прогін агента не йде) | 1 |
| `sets/l12.jsonl` | кейс `JNT-026` для спільної перевірки: клієнт `CUS-0006`, транзакція `TX-0601` | 3, 5 |
| `loader.py`, `runner.py`, `assertions.py`, `stand.py`, `judge.py`, `console.py` | ранер L03–L09: лоадер, прогін кейса, драбина assertion'ів, клієнт стенду, суддя, друк | 1, 3 |
| `cli.py`, `validate.py`, `generate_from_engines.py`, `tools/` | колишні точки входу: `cli.py --set …`, `--dry-run`, генерація кейсів із рушіїв, калібрування й порівняння суддів | 1 |
| `docker-compose.yml`, `Dockerfile` | одна команда: `docker compose run --rm eval` → `python run_eval.py` | 2–6 |
| `Makefile` | обгортка для bash і macOS: `make eval`, `make dry-run`, `make compare` | 6 |
| `.gitattributes` | набори й записи — завжди з LF, на будь-якій ОС | 6 |
| `reports/` | сюди лягають записи прогонів `run-<час>.json` | 4–6 |
| `eval-framework.template.md` | шаблон артефакту: скопіюйте в `eval-framework.md` | 1–6, ДЗ |

## Підготовка

Розкладка тек, як на попередніх заняттях:

```text
~/paypilot/paypilot-stand    стенд
~/paypilot/l11               кіт L11 з вашими tests/manual і tests/generated
~/paypilot/l12               цей кіт
```

```bash
git clone https://github.com/TarasFedorenkoFTV/paypilot-l12-eval ~/paypilot/l12
cd ~/paypilot/l12
cp .env.example .env
```

```powershell
# Windows PowerShell: ~ у git clone не розгортається
git clone https://github.com/TarasFedorenkoFTV/paypilot-l12-eval "$HOME\paypilot\l12"
cd "$HOME\paypilot\l12"
Copy-Item .env.example .env
```

Якщо ваші тести L11 лежать не в `~/paypilot/l11` (наприклад, у репозиторії ДЗ L11), впишіть шлях у `.env`: `L11_DIR=../назва-теки`.

Стенд має працювати зі справжньою моделлю: у `.env` стенду `LLM_PROVIDER=anthropic` і ваш `ANTHROPIC_API_KEY`. Перевірка: `docker exec paypilot-stand-stand-1 python scripts/doctor.py` → `llm provider [OK] anthropic`.

Перший запуск у кожній теці збирає образ (Python 3.12 і pytest) — близько хвилини. `curl` у командах нижче — `curl.exe`: у Windows PowerShell 5.1 просто `curl` — інша команда. Потім:

```bash
docker compose run --rm eval --dry-run
```

На свіжому кіті команда падає з `NotImplementedError: КРОК 2: set_hash() ще не написано`. Так і має бути: хеш набору — ваш перший шматок коду.

## Команди за кроками

Усі команди — з кореня кіта. Змінні передаються через `-e`: так однаково працює в bash, Git Bash і PowerShell.

| Крок | Команда |
| --- | --- |
| 2. Тести кроку | `docker compose run --rm --entrypoint pytest eval tests -q -k step2` |
| 3. Рахунок без викликів | `docker compose run --rm eval --dry-run` |
| 3. Рахунок з релізним шаром | `docker compose run --rm -e GATE=all eval --dry-run` |
| 3. Один кейс спільної перевірки | `docker compose run --rm -e SET=l12 eval` |
| 4. Прогін набору L03 | `docker compose run --rm -e SET=l03 eval` |
| 4–6. Два записи поруч | `docker compose run --rm eval --compare reports/<A>.json reports/<B>.json` |
| 5. Спільна перевірка з дефектом | `docker compose run --rm -e SET=l12 -e DEFECTS=D26 eval` |
| 6. Увесь контур (щоденний шар) | `docker compose run --rm eval` |
| 2–6. Усі тести кроків | `docker compose run --rm --entrypoint pytest eval tests -q` |

`make eval`, `make dry-run`, `make compare A=… B=…` роблять те саме в bash і macOS; на Windows `make` зазвичай немає, і він не потрібен.

Змінні контуру:

| Змінна | Типово | Що задає |
| --- | --- | --- |
| `SET` | `all` (L03–L08 і L12) | набори через кому: `l03`, `l03,l07`, … |
| `GATE` | `daily` | шар: `daily`, `release` або `all`; суддя живе лише в `release` |
| `CASE` | — | лише ці кейси, через кому |
| `PROFILE` | `clean` | профіль стенду на час прогону |
| `DEFECTS` | — | додаткові дефекти на час прогону, наприклад `D26`; після прогону знімаються |
| `JUDGE_MODEL` | — | модель судді з L10, у `.env`; потрібна лише для релізного шару |

Код повернення: `0` — усе зелене; `1` — щось упало, пропущене, червоні або не знайдені unit-тести L11; `2` — стенд недоступний. `--compare` повертає `0`, коли записи порівнювані, і `1`, коли ні.

## Стенд на base.v4 і назад (крок 4)

`base.v4.md` лежить у кіті L11 (`agent-prompt/`). З теки стенду:

```bash
cd ~/paypilot/paypilot-stand
docker compose -f docker-compose.yml -f ../l11/agent-prompt/compose.v4.yml up -d
curl.exe -s http://localhost:8000/api/_test/prompt
```

Повернутися на base.v1: `docker compose up -d`.

Стенд піднімається 10–15 секунд. Мітка, яку стенд віддає в полі `version`, лишається `base.v1` за будь-якого файлу: ім'я зашите в стенді. Яка версія діє, видно лише в першому рядку тексту промпту.

## Ціна прогонів

Еталонні прогони 12.11.2026, стенд `74f1995`, агент `claude-haiku-5-5`:

| Прогін | Кейсів | Викликів агента | Час | Гроші |
| --- | --- | --- | --- | --- |
| `--dry-run` | — | 0 | секунди | $0 |
| `SET=l03` | 37 | 37 | ≈ 2,7 хв | ≈ $0.015 |
| `SET=l12` | 1 | 1 | ≈ 6 с | < $0.001 |
| увесь контур, щоденний шар | 71 | 72 | 5–6 хв | ≈ $0.03 |

Суддя на щоденному шарі не викликається. Відповіді моделі щоразу трохи різні: частина вердиктів на однакових умовах може змінитися між прогонами.

## Чого не робити

- Не редагуйте `base.v1.md` і `base.v4.md`: їх лише перемикають.
- Не правте кейси в `sets/`, щоб прогін «позеленів». Якщо кейс здається хибним, напишіть про це в шаблоні.
- Не запускайте прогін на спільному стенді: ранер скидає базу стенду перед кожним кейсом і перемикає профіль.
- Не комітьте `.env` і ключі.
