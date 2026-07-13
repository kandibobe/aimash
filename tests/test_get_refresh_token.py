"""scripts/get_refresh_token.py — режим --remote (согласие даёт ЧУЖОЙ браузер) и запись в .env.

Сеть/браузер не трогаем: Flow подменяем. Тестируем то, что реально ломается:
  • _extract_code — что именно присылает владелец аккаунта: полная строка адресной строки, голый код,
    строка с error=access_denied (доступ не разрешён), мусор;
  • _remote_credentials — код из ответа доходит до fetch_token, а сам код (одноразовый credential)
    и refresh-токен НЕ печатаются в stdout (golden rule #5);
  • save_to_env — перезапись существующей строки и дописывание отсутствующей (без дублей).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("google_auth_oauthlib")  # дев-зависимость OAuth-флоу; нет → скип модуля

import scripts.get_refresh_token as grt  # noqa: E402

CODE = "4/0AVMBsJi-fake-authorization-code"
REDIRECT = (
    f"http://localhost:8765/?state=xyz&code={CODE}&scope=https://www.googleapis.com/auth/drive.file"
)


def test_extract_code_from_redirect_url() -> None:
    assert grt._extract_code(REDIRECT) == CODE


def test_extract_code_from_bare_code() -> None:
    assert grt._extract_code(f"  {CODE}\n") == CODE


def test_extract_code_access_denied_is_none() -> None:
    assert grt._extract_code("http://localhost:8765/?error=access_denied&state=xyz") is None


@pytest.mark.parametrize("junk", ["", "   ", "ок, сделал", "http://localhost:8765/"])
def test_extract_code_junk_is_none(junk: str) -> None:
    assert grt._extract_code(junk) is None


class _FakeCreds:
    refresh_token = "1//fake-refresh-token"
    scopes = grt.SHEETS_SCOPES


class _FakeFlow:
    """Подмена google_auth_oauthlib.flow.Flow — фиксируем, с чем звали."""

    last: _FakeFlow | None = None

    def __init__(self) -> None:
        self.fetched: str | None = None
        self.credentials = _FakeCreds()

    @classmethod
    def from_client_config(cls, client_config, scopes, redirect_uri):  # noqa: ANN001
        flow = cls()
        flow.client_config, flow.scopes, flow.redirect_uri = client_config, scopes, redirect_uri
        _FakeFlow.last = flow
        return flow

    def authorization_url(self, **kwargs):  # noqa: ANN003
        self.auth_kwargs = kwargs
        return "https://accounts.google.com/o/oauth2/auth?fake=1", "state-123"

    def fetch_token(self, code: str) -> None:
        self.fetched = code


def test_remote_credentials_exchanges_code_and_leaks_nothing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(grt, "Flow", _FakeFlow)
    monkeypatch.setattr("builtins.input", lambda *_: REDIRECT)

    creds = grt._remote_credentials({"installed": {}}, grt.SHEETS_SCOPES)

    flow = _FakeFlow.last
    assert flow is not None
    assert (
        flow.redirect_uri == grt.REMOTE_REDIRECT
    )  # порт фиксирован: он попадает в ссылку согласия
    assert flow.auth_kwargs == {
        "access_type": "offline",
        "prompt": "consent",
    }  # иначе не будет refresh
    assert flow.fetched == CODE
    assert creds.refresh_token == _FakeCreds.refresh_token

    out = capsys.readouterr().out
    assert "https://accounts.google.com/o/oauth2/auth?fake=1" in out  # ссылку — печатаем
    assert CODE not in out and _FakeCreds.refresh_token not in out  # секреты — никогда


def test_remote_credentials_exits_when_no_code(monkeypatch, capsys) -> None:
    monkeypatch.setattr(grt, "Flow", _FakeFlow)
    monkeypatch.setattr("builtins.input", lambda *_: "http://localhost:8765/?error=access_denied")

    with pytest.raises(SystemExit) as exc:
        grt._remote_credentials({"installed": {}}, grt.SHEETS_SCOPES)

    assert exc.value.code == 1
    assert _FakeFlow.last.fetched is None  # обмена не было
    assert "error=access_denied" in capsys.readouterr().out  # подсказка, что делать


def test_save_to_env_appends_and_replaces(monkeypatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=xxx\nSHEETS_OWNER_EMAIL=myhalads@gmail.com\n", encoding="utf-8")
    monkeypatch.setattr(grt, "ENV_PATH", env)

    grt.save_to_env("tok-1", "SHEETS_REFRESH_TOKEN")
    grt.save_to_env("tok-2", "SHEETS_REFRESH_TOKEN")  # повтор → перезапись, не дубль

    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines.count("SHEETS_REFRESH_TOKEN=tok-2") == 1
    assert not any(line.startswith("SHEETS_REFRESH_TOKEN=tok-1") for line in lines)
    assert "BOT_TOKEN=xxx" in lines  # чужие строки не тронуты
    assert "SHEETS_OWNER_EMAIL=myhalads@gmail.com" in lines
