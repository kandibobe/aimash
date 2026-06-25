# Aimash — частые команды. Запуск из Git Bash / WSL (`make <target>`).
# PowerShell-эквиваленты — в README. Python-команды через `python` (venv должен быть активен).
.DEFAULT_GOAL := help
.PHONY: help install hooks lint fmt typecheck test run db-up db-down refresh-token check-access mcp-list

help:           ## Список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:        ## Установить зависимости (dev) + pre-commit хуки
	pip install -e ".[dev]"
	pre-commit install

hooks:          ## Прогнать pre-commit по всем файлам (gitleaks + ruff)
	pre-commit run --all-files

lint:           ## Ruff lint
	ruff check .

fmt:            ## Ruff format
	ruff format .

typecheck:      ## mypy
	mypy core ads agent confirm bot db

test:           ## Офлайн safety-тесты
	pytest -q

run:            ## Запустить Telegram-бота (нужен .env)
	python -m bot.main

db-up:          ## Поднять dev-Postgres (docker)
	docker compose up -d postgres

db-down:        ## Остановить dev-Postgres
	docker compose down

refresh-token:  ## Получить Google Ads refresh token (OAuth desktop)
	python scripts/get_refresh_token.py

check-access:   ## Проверить доступ к Google Ads (read-only, аккаунт Aimash Draft)
	python scripts/check_access.py

mcp-list:       ## Статус MCP-серверов
	claude mcp list
