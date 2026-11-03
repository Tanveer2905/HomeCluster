"""The 5-field cron matcher in ``coordinator/schedules.py`` (SPEC §10)."""

from __future__ import annotations

from datetime import datetime

import pytest

from coordinator.schedules import Cron, CronError, matches, next_run, validate


def dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ parsing
def test_every_minute() -> None:
    cron = Cron("* * * * *")
    assert cron.matches(dt("2026-09-10 13:37"))
    assert cron.matches(dt("2026-01-01 00:00"))


def test_fixed_time() -> None:
    cron = Cron("30 2 * * *")
    assert cron.matches(dt("2026-09-10 02:30"))
    assert not cron.matches(dt("2026-09-10 02:31"))
    assert not cron.matches(dt("2026-09-10 03:30"))


def test_lists_and_ranges() -> None:
    cron = Cron("0,30 9-17 * * *")
    assert cron.matches(dt("2026-09-10 09:00"))
    assert cron.matches(dt("2026-09-10 17:30"))
    assert not cron.matches(dt("2026-09-10 18:00"))
    assert not cron.matches(dt("2026-09-10 09:15"))


def test_steps() -> None:
    cron = Cron("*/10 * * * *")
    assert cron.minutes == {0, 10, 20, 30, 40, 50}
    assert cron.matches(dt("2026-09-10 12:20"))
    assert not cron.matches(dt("2026-09-10 12:25"))
    assert Cron("5-25/10 * * * *").minutes == {5, 15, 25}
    assert Cron("0 */6 * * *").hours == {0, 6, 12, 18}


def test_day_of_week() -> None:
    # 2026-09-10 is a Thursday (cron dow 4), 2026-09-13 a Sunday (dow 0).
    assert Cron("0 8 * * 4").matches(dt("2026-09-10 08:00"))
    assert not Cron("0 8 * * 5").matches(dt("2026-09-10 08:00"))
    assert Cron("0 8 * * 0").matches(dt("2026-09-13 08:00"))
    assert Cron("0 8 * * 7").matches(dt("2026-09-13 08:00"))  # 7 == Sunday
    assert Cron("0 8 * * mon-fri").matches(dt("2026-09-10 08:00"))


def test_month_names_and_numbers() -> None:
    assert Cron("0 0 1 sep *").matches(dt("2026-09-01 00:00"))
    assert Cron("0 0 1 9 *").matches(dt("2026-09-01 00:00"))
    assert not Cron("0 0 1 10 *").matches(dt("2026-09-01 00:00"))


def test_dom_or_dow_when_both_restricted() -> None:
    """Vixie semantics: restricted dom AND dow match if either does."""
    cron = Cron("0 0 13 * 4")  # the 13th, or any Thursday
    assert cron.matches(dt("2026-09-13 00:00"))  # a Sunday, but the 13th
    assert cron.matches(dt("2026-09-10 00:00"))  # a Thursday, not the 13th
    assert not cron.matches(dt("2026-09-11 00:00"))


def test_dom_only() -> None:
    cron = Cron("0 0 15 * *")
    assert cron.matches(dt("2026-09-15 00:00"))
    assert not cron.matches(dt("2026-09-16 00:00"))


# ------------------------------------------------------------------- errors
@pytest.mark.parametrize(
    "expr",
    [
        "",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "abc * * * *",
        "*/0 * * * *",
        "5-1 * * * *",
        ", * * * *",
    ],
)
def test_invalid_expressions(expr: str) -> None:
    with pytest.raises(CronError):
        Cron(expr)


def test_validate_normalises_whitespace() -> None:
    assert validate("  0   9  *  *  1 ") == "0 9 * * 1"


# ----------------------------------------------------------------- next_run
def test_next_run_is_strictly_in_the_future() -> None:
    base = dt("2026-09-10 12:00").timestamp()
    nxt = next_run("* * * * *", base)
    assert nxt == dt("2026-09-10 12:01").timestamp()


def test_next_run_skips_to_the_next_match() -> None:
    base = dt("2026-09-10 12:05").timestamp()
    nxt = datetime.fromtimestamp(next_run("0 3 * * *", base))
    assert nxt == dt("2026-09-11 03:00")


def test_next_run_crosses_a_year() -> None:
    base = dt("2026-09-10 12:00").timestamp()
    nxt = datetime.fromtimestamp(next_run("0 0 1 1 *", base))
    assert nxt == dt("2027-01-01 00:00")


def test_next_run_seconds_are_dropped() -> None:
    base = dt("2026-09-10 12:00").timestamp() + 42.7
    nxt = datetime.fromtimestamp(next_run("* * * * *", base))
    assert nxt == dt("2026-09-10 12:01")


def test_module_helpers() -> None:
    assert matches("* * * * *", dt("2026-09-10 00:00")) is True
    assert next_run("* * * * *") > 0
    assert repr(Cron("* * * * *")) == "Cron('* * * * *')"
