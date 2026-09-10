from datetime import UTC, datetime, timedelta, timezone

from agent.automatic import active_window


def _fixed(**overrides):
    policy = {
        "id": "policy-1",
        "consent_status": "accepted",
        "rule_type": "fixed_schedule",
        "project_id": "project-1",
        "schedule": {"mon": [{"start": "09:00", "end": "17:00"}]},
    }
    policy.update(overrides)
    return policy


def test_fixed_schedule_uses_employee_local_weekday_and_timezone():
    local = timezone(timedelta(hours=5))

    window = active_window(_fixed(), datetime(2026, 9, 7, 9, 30, tzinfo=local))

    assert window is not None
    assert window.project_id == "project-1"
    assert window.ends_at == datetime(2026, 9, 7, 17, 0, tzinfo=local)
    assert active_window(_fixed(), datetime(2026, 9, 7, 17, 0, tzinfo=local)) is None


def test_fixed_schedule_supports_overnight_windows_from_previous_day():
    policy = _fixed(schedule={"mon": [{"start": "22:00", "end": "06:00"}]})

    window = active_window(policy, datetime(2026, 9, 8, 2, 0, tzinfo=UTC))

    assert window is not None
    assert window.ends_at == datetime(2026, 9, 8, 6, 0, tzinfo=UTC)


def test_shift_policy_uses_shift_project_and_rejects_bad_or_expired_windows():
    policy = {
        "id": "policy-2",
        "consent_status": "accepted",
        "rule_type": "shifts",
        "project_id": "fallback",
        "shift_windows": [
            {"starts_at": "bad", "ends_at": "bad", "project_id": "bad"},
            {
                "starts_at": "2026-09-08T08:00:00+00:00",
                "ends_at": "2026-09-08T16:00:00+00:00",
                "project_id": "shift-project",
            },
        ],
    }

    window = active_window(policy, datetime(2026, 9, 8, 12, 0, tzinfo=UTC))

    assert window is not None
    assert window.project_id == "shift-project"
    assert active_window(policy, datetime(2026, 9, 8, 16, 0, tzinfo=UTC)) is None


def test_policy_never_acts_before_explicit_acceptance():
    moment = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)

    assert active_window(_fixed(consent_status="pending"), moment) is None
    assert active_window(_fixed(consent_status="declined"), moment) is None
