from datetime import date

import pytest

from dto_tui import presets, resolve

TODAY = date(2026, 10, 26)  # a Monday


def test_week_is_monday_to_sunday_with_exclusive_end():
    p = resolve("week", date(2026, 10, 15), today=TODAY)  # Thursday
    assert p.display.start == date(2026, 10, 12)
    assert p.display.end == date(2026, 10, 19)
    assert p.display.last_day == date(2026, 10, 18)
    assert p.display.granularity == "DAILY"
    assert p.previous.start == date(2026, 10, 5)
    assert p.previous.end == date(2026, 10, 12)


def test_current_week_is_clipped_to_today_and_previous_matches_length():
    p = resolve("week", today=TODAY)
    assert p.display.start == TODAY
    assert p.query.end == date(2026, 10, 27)
    assert p.query.days == 1
    assert p.previous.start == date(2026, 10, 19)
    assert p.previous.days == 1


def test_month_handles_year_boundary():
    p = resolve("month", date(2026, 1, 20), today=TODAY)
    assert p.display.start == date(2026, 1, 1)
    assert p.display.end == date(2026, 2, 1)
    assert p.previous.start == date(2025, 12, 1)
    assert p.previous.end == date(2026, 1, 1)


def test_custom_inclusive_end_and_granularity():
    p = resolve("custom", date(2026, 1, 1), date(2026, 1, 31), today=TODAY)
    assert p.display.end == date(2026, 2, 1)
    assert p.display.granularity == "DAILY"
    assert p.previous.start == date(2025, 12, 1)

    long = resolve("custom", date(2025, 1, 1), date(2025, 12, 31), today=TODAY)
    assert long.display.granularity == "MONTHLY"


def test_future_period_has_empty_query_window():
    p = resolve("month", date(2027, 3, 1), today=TODAY)
    assert p.query.is_empty


@pytest.mark.parametrize("start,end", [(None, date(2026, 1, 1)), (date(2026, 2, 1), date(2026, 1, 1))])
def test_custom_validation(start, end):
    with pytest.raises(ValueError):
        resolve("custom", start, end, today=TODAY)


def test_presets():
    p = presets(TODAY)
    assert p["Last 7 days"] == (date(2026, 10, 19), date(2026, 10, 25))
    assert p["Year to date"] == (date(2026, 1, 1), TODAY)
    assert p["Last 12 months"] == (date(2025, 10, 1), date(2026, 9, 30))
