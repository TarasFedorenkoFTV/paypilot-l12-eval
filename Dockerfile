# Образ контуру: Python 3.12 і pytest для unit-тестів L11. Будується сам
# під час першого `docker compose run --rm eval`.
FROM python:3.12-slim
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore pytest==9.2.0
