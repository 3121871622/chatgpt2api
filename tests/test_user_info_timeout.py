from services.openai_backend_api import OpenAIBackendAPI


def test_get_user_info_passes_short_timeout_to_all_backend_calls(monkeypatch):
    api = object.__new__(OpenAIBackendAPI)
    api.access_token = "token"
    seen = []

    def fake_me(timeout):
        seen.append(("me", timeout))
        return {"email": "user@example.test", "id": "user-1"}

    def fake_init(timeout):
        seen.append(("init", timeout))
        return {"default_model_slug": "auto"}

    def fake_account(timeout):
        seen.append(("account", timeout))
        return {"plan_type": "free"}

    monkeypatch.setattr(api, "_get_me", fake_me)
    monkeypatch.setattr(api, "_get_conversation_init", fake_init)
    monkeypatch.setattr(api, "_get_default_account", fake_account)

    result = api.get_user_info(timeout=8)

    assert result["email"] == "user@example.test"
    # get_user_info submits the three probes to a thread pool, so their append
    # order depends on scheduling. Assert the set of calls, not their order —
    # the old ordered assertion failed intermittently under load.
    assert sorted(seen) == [("account", 8), ("init", 8), ("me", 8)]
    assert all(timeout == 8 for _name, timeout in seen)
