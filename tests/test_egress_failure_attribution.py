from services.proxy_health import EGRESS_FAILURE_CODES, ProxyHealthRegistry


def test_stream_failures_are_attributed_to_the_egress():
    """SSE stream breakage says the egress path broke, not that the account is bad.

    Before 1.9.20 an interrupted image stream only moved the request to another
    account: the proxy node that kept resetting streams stayed "healthy" and
    kept receiving traffic, which is what the 160-240s slow calls looked like.
    """
    assert "image_stream_interrupted" in EGRESS_FAILURE_CODES
    assert "image_stream_timeout" in EGRESS_FAILURE_CODES


def test_account_level_failures_stay_out_of_egress_health():
    for code in ("quota_exhausted", "token_invalid", "no_available_account", "content_policy"):
        assert code not in EGRESS_FAILURE_CODES


def test_stream_failure_threshold_override_cools_after_five_breaks():
    """Every node of the image egress group broke 7-10% of streams, so a
    three-strike rule made all five flap together; stream codes need 5."""
    now = [1000.0]
    registry = ProxyHealthRegistry(failure_threshold=3, clock=lambda: now[0])
    key = "group:pg-cc90e14b27:node-test"
    for _ in range(4):
        registry.report_failure(key, "sse reset", failure_threshold=5)
    assert registry.is_available(key) is True
    registry.report_failure(key, "sse reset", failure_threshold=5)
    assert registry.is_available(key) is False
    now[0] += 61
    assert registry.is_available(key) is True


def test_connection_failures_keep_the_default_threshold():
    now = [1000.0]
    registry = ProxyHealthRegistry(failure_threshold=3, clock=lambda: now[0])
    key = "group:pg-cc90e14b27:node-test2"
    for _ in range(2):
        registry.report_failure(key, "connect refused")
    assert registry.is_available(key) is True
    registry.report_failure(key, "connect refused")
    assert registry.is_available(key) is False
