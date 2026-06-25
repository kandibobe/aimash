"""Модели БД (SQLAlchemy 2.0). Скелет — наполняется в фазе 0/1 + миграции Alembic.

Таблицы:
- whitelist        — разрешённые chat_id
- user_settings    — расписание отчётов, пороги алертов, выбранная модель
- proposals        — очередь черновиков изменений (diff «было→станет», статус)
- audit_log        — все операции: кто/когда/что/результат, по confirmation_id (без секретов)
- oauth_tokens     — refresh-токены аккаунтов, ШИФРОВАННЫЕ at-rest (core.secrets)
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# TODO(фаза 0/1): объявить таблицы выше как классы Base; токены хранить через core.secrets.encrypt.
