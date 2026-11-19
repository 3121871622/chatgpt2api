from services.account_service import account_service


def test_wait_for_rotated_access_token_returns_empty_when_unchanged(monkeypatch):
    monkeypatch.setattr(account_service, "resolve_access_token", lambda token: token)
    assert account_service.wait_for_rotated_access_token(
        "tok", budget_seconds=0.05, poll_seconds=0.01
    ) == ""


def test_wait_for_rotated_access_token_stops_when_rotation_commits(monkeypatch):
    calls = {"n": 0}

    def resolve(token):
        calls["n"] += 1
        return "tok-new" if calls["n"] > 1 else token

    monkeypatch.setattr(account_service, "resolve_access_token", resolve)
    assert account_service.wait_for_rotated_access_token(
        "tok", budget_seconds=1.0, poll_seconds=0.01
    ) == "tok-new"
    assert calls["n"] >= 2


def test_wait_for_rotated_access_token_returns_new_token_immediately(monkeypatch):
    monkeypatch.setattr(account_service, "resolve_access_token", lambda token: "tok-new")
    assert account_service.wait_for_rotated_access_token(
        "tok", budget_seconds=0.5, poll_seconds=0.01
    ) == "tok-new"


def test_wait_for_rotated_access_token_handles_empty_token(monkeypatch):
    monkeypatch.setattr(account_service, "resolve_access_token", lambda token: "unused")
    assert account_service.wait_for_rotated_access_token("") == ""
