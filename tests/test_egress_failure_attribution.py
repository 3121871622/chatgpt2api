from services.proxy_health import EGRESS_FAILURE_CODES


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
