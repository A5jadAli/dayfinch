import time
from pathlib import Path

import httpx
import pytest

from scripts.load_test import (
    LoadTestError,
    Measurements,
    _login_web_reader,
    parser,
    read_tokens,
    validate_target,
    web_reader,
)


def test_load_target_requires_https_and_remote_acknowledgement():
    assert validate_target("http://127.0.0.1:8000", False) == "http://127.0.0.1:8000"
    with pytest.raises(LoadTestError, match="HTTPS"):
        validate_target("http://tracker.example.test", True)
    with pytest.raises(LoadTestError, match="acknowledge-production-impact"):
        validate_target("https://tracker.example.test", False)
    assert validate_target("https://tracker.example.test/", True) == (
        "https://tracker.example.test"
    )


def test_token_file_is_deduplicated_and_never_accepts_short_values(tmp_path: Path):
    token_file = tmp_path / "tokens"
    token_file.write_text(f"# devices\n{'a' * 40}\n{'a' * 40}\n{'b' * 40}\n")
    assert read_tokens(token_file) == ["a" * 40, "b" * 40]
    token_file.write_text("too-short\n")
    with pytest.raises(LoadTestError, match="one valid device token"):
        read_tokens(token_file)


def test_measurement_report_calculates_errors_and_percentiles():
    measurements = Measurements()
    for elapsed, status in ((0.01, 200), (0.02, 201), (0.03, 500), (0.04, None)):
        measurements.record("/endpoint", elapsed, status)
    report = measurements.report(2, 3)
    assert report["attempts"] == 4
    assert report["errors"] == 2
    assert report["error_percent"] == 50
    assert report["endpoints"]["/endpoint"]["p95_ms"] == 40


def test_harness_defaults_match_api_and_include_web_reader_options():
    arguments = parser().parse_args(["--tokens-file", "tokens"])
    assert arguments.heartbeat_interval == 15
    assert arguments.web_password_env == "TRACKER_LOAD_TEST_PASSWORD"
    assert arguments.read_interval == 1


@pytest.mark.anyio
async def test_web_reader_authenticates_and_measures_all_read_paths():
    requested: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/login":
            return httpx.Response(
                200,
                text='<input type="hidden" name="csrf" value="test-csrf">',
            )
        if request.method == "POST" and request.url.path == "/login":
            assert b"password=not-printed" in request.content
            return httpx.Response(303, headers={"location": "/"})
        return httpx.Response(200, text="ok")

    measurements = Measurements()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    ) as client:
        await _login_web_reader(client, "reader@example.test", "not-printed")
        await web_reader(
            client,
            measurements,
            stop_at=time.monotonic() + 0.02,
            read_interval=0.01,
        )

    assert {path for method, path in requested if method == "GET"} >= {
        "/",
        "/timesheets",
        "/reports",
    }
    assert set(measurements.latencies) == {"/", "/timesheets", "/reports"}
