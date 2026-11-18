from services.account_service import account_service


def test_recover_after_refresh_race_returns_new_token(monkeypatch):
    released: list[str] = []
    monkeypatch.setattr(account_service, "resolve_access_token", lambda token: "new-token")
    monkeypatch.setattr(account_service, "release_image_slot", lambda token: released.append(token))
    monkeypatch.setattr(account_service, "probe_access_token_light", lambda token, timeout=5.0: True)
    inflight: dict[str, int] = {}
    monkeypatch.setattr(account_service, "_image_inflight", inflight)
    token = account_service._recover_after_refresh_race("old-token")
    assert token == "new-token"
    assert inflight.get("new-token") == 1
    assert released == ["old-token"]


def test_recover_after_refresh_race_returns_none_when_token_unchanged(monkeypatch):
    released: list[str] = []
    monkeypatch.setattr(account_service, "resolve_access_token", lambda token: token)
    monkeypatch.setattr(account_service, "release_image_slot", lambda token: released.append(token))
    inflight: dict[str, int] = {}
    monkeypatch.setattr(account_service, "_image_inflight", inflight)
    assert account_service._recover_after_refresh_race("old-token") is None
    assert released == ["old-token"]
    assert inflight == {}


def test_recover_after_refresh_race_releases_new_slot_when_probe_fails(monkeypatch):
    released: list[str] = []

    def failing_probe(token, timeout=5.0):
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr(account_service, "resolve_access_token", lambda token: "new-token")
    monkeypatch.setattr(account_service, "release_image_slot", lambda token: released.append(token))
    monkeypatch.setattr(account_service, "probe_access_token_light", failing_probe)
    inflight: dict[str, int] = {}
    monkeypatch.setattr(account_service, "_image_inflight", inflight)
    assert account_service._recover_after_refresh_race("old-token") is None
    # release_image_slot is stubbed here, so the bookkeeping it normally does is
    # replaced by the recorded call list; the new token's slot must be released.
    assert released == ["old-token", "new-token"]
