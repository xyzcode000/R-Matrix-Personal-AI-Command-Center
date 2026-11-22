from datetime import datetime, timezone

from app.time_context import (
    answer_simple_time_question,
    build_time_context,
    current_time,
    resolve_relative_dates,
)


def test_current_time_uses_asia_kolkata_and_keeps_current_year():
    now = datetime(2025, 12, 31, 18, 45, tzinfo=timezone.utc)
    localized = current_time(now)

    assert localized.isoformat() == "2026-01-01T00:15:00+05:30"
    context = build_time_context(now=now)
    assert "Thursday, 01 January 2026 at 00:15:00" in context
    assert "current year: 2026" in context
    assert "timezone: Asia/Kolkata" in context


def test_relative_dates_resolve_across_year_boundary():
    now = datetime(2025, 12, 31, 18, 45, tzinfo=timezone.utc)
    resolved = {
        item.expression.casefold(): item
        for item in resolve_relative_dates(
            "today, tomorrow, yesterday, next Monday, in 3 days, next week, and end of this month",
            now=now,
        )
    }

    assert resolved["today"].start.strftime("%Y-%m-%d") == "2026-01-01"
    assert resolved["tomorrow"].start.strftime("%Y-%m-%d") == "2026-01-02"
    assert resolved["yesterday"].start.strftime("%Y-%m-%d") == "2025-12-31"
    assert resolved["next monday"].start.strftime("%Y-%m-%d") == "2026-01-05"
    assert resolved["in 3 days"].start.strftime("%Y-%m-%d") == "2026-01-04"
    assert resolved["next week"].start.strftime("%Y-%m-%d") == "2026-01-05"
    assert resolved["next week"].end.strftime("%Y-%m-%d") == "2026-01-11"
    assert resolved["end of this month"].start.strftime("%Y-%m-%d") == "2026-01-31"
    assert resolved["end of this month"].end.strftime("%H:%M:%S") == "23:59:59"


def test_month_end_handles_leap_year_and_december_rollover():
    leap_day = resolve_relative_dates(
        "end of this month",
        now=datetime(2024, 2, 28, 12, tzinfo=timezone.utc),
    )[0]
    december = resolve_relative_dates(
        "end of this month",
        now=datetime(2026, 12, 15, 12, tzinfo=timezone.utc),
    )[0]

    assert leap_day.start.strftime("%Y-%m-%d") == "2024-02-29"
    assert december.end.strftime("%Y-%m-%d") == "2026-12-31"


def test_next_week_starts_on_following_monday_when_today_is_monday():
    resolved = resolve_relative_dates(
        "next Monday and next week",
        now=datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc),
    )

    assert resolved[0].start.strftime("%Y-%m-%d") == "2026-11-16"
    assert resolved[1].start.strftime("%Y-%m-%d") == "2026-11-16"
    assert resolved[1].end.strftime("%Y-%m-%d") == "2026-11-22"


def test_time_context_includes_resolved_phrases_for_model():
    context = build_time_context(
        now=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc),
        text="Schedule a check tomorrow and another in 3 days, at end of this month.",
    )

    assert "'tomorrow' means Tuesday, 10 November 2026" in context
    assert "'in 3 days' means Thursday, 12 November 2026" in context
    assert "'end of this month' spans Monday, 30 November 2026 through Monday, 30 November 2026" in context


def test_simple_time_questions_are_answered_from_the_backend_clock():
    now = datetime(2026, 11, 9, 12, 45, tzinfo=timezone.utc)

    assert answer_simple_time_question("whats the date today", now=now) == (
        "Today is Monday, 9 November 2026."
    )
    assert answer_simple_time_question("What is tomorrow's date?", now=now) == (
        "Tomorrow is Tuesday, 10 November 2026."
    )
    assert answer_simple_time_question("What date was yesterday?", now=now) == (
        "Yesterday was Sunday, 8 November 2026."
    )
    assert answer_simple_time_question("What day is today?", now=now) == "Today is Monday."
    assert answer_simple_time_question("What time is it?", now=now) == (
        "It is 6:15 PM Asia/Kolkata (UTC+05:30) on Monday, 9 November 2026."
    )
    assert answer_simple_time_question("Explain what a hash map is.", now=now) is None


def test_explicit_date_questions_are_answered_from_the_backend_clock():
    now = datetime(2026, 11, 9, 12, 45, tzinfo=timezone.utc)

    assert answer_simple_time_question("What day of the week is 25 December 2026?", now=now) == (
        "25 December 2026 is a Friday."
    )
    assert answer_simple_time_question("What day is December 25, 2026?", now=now) == (
        "25 December 2026 is a Friday."
    )
    assert answer_simple_time_question("Which weekday was 2020-01-01?", now=now) == (
        "1 January 2020 was a Wednesday."
    )
    assert answer_simple_time_question("What day of the week is 31 February 2027?", now=now) is None
