from pathlib import Path

import pytest

from scripts.load_test import LoadTestError, Measurements, read_tokens, validate_target


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
