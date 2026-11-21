from resilinet.observability import redact_path


def test_action_tokens_are_redacted_from_logged_paths() -> None:
    assert redact_path("/actions/AbC123secret-token") == "/actions/{token}"
    assert redact_path("/actions/") == "/actions/{token}"


def test_ordinary_paths_are_unchanged() -> None:
    assert redact_path("/api/v1/health") == "/api/v1/health"
    assert redact_path("/operator/reviews") == "/operator/reviews"
    assert redact_path("/replay/lite") == "/replay/lite"


def test_uvicorn_access_log_redacts_tokens_and_query_strings() -> None:
    import logging

    from resilinet.observability import AccessLogFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/actions/super-secret?token=also-secret", "1.1", 200),
        None,
    )

    assert AccessLogFilter().filter(record) is True
    assert record.args[2] == "/actions/{token}"
    assert "secret" not in str(record.args)
