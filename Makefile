# Обгортка для bash / macOS / Linux. Головна форма — docker compose (див. README).
#   make eval                       = docker compose run --rm eval
#   make eval SET=l12 DEFECTS=D26   змінні передаються в контейнер
#   make dry-run
#   make compare A=reports/run-…json B=reports/run-…json
export SET GATE CASE PROFILE DEFECTS JUDGE_MODEL

.PHONY: eval dry-run compare
eval:
	docker compose run --rm eval

dry-run:
	docker compose run --rm eval --dry-run

compare:
	docker compose run --rm eval --compare $(A) $(B)
