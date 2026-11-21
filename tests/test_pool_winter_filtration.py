"""Winter still filters two hours a day: the sun first, the off-peak night after."""

import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.loads.pool import (
    Inputs,
    Thresholds,
    decide,
    minutes_to_hc_end,
    winter_stir_owed,
    winter_top_up_due,
)
from logic.router_state import PoolMemory

TZ = timezone(timedelta(hours=1))


def night(hour: int, minute: int = 0, *, filtered: float = 0.0, stirred: float = 30.0,
          **kw) -> Inputs:
    """A winter night, off-peak, pump stopped, cold water — stirred already, by default."""
    now = datetime(2026, 12, 10, hour, minute, tzinfo=TZ)
    if hour < 6:
        now += timedelta(days=1)
    base = Inputs(now=now, daylight=False, grid_w=300.0, pump_speed=0, water_temp_c=12.0,
                  air_temp_c=3.0, mode="auto", v3_started_at=None, v3_last_ended_at=None,
                  tempo_color="Bleu", is_hc=True, season="hiver", filtered_today_min=filtered,
                  stirred_today_min=stirred)
    return replace(base, **kw)


def test_nothing_runs_while_the_window_can_still_hold_the_top_up():
    assert decide(night(3, 30, filtered=60.0), Thresholds()).target_speed == 0


def test_the_top_up_starts_so_as_to_end_with_the_window():
    decision = decide(night(5, 0, filtered=60.0), Thresholds())
    assert decision.target_speed == 1
    assert "complément" in decision.reason


def test_a_day_with_no_sun_runs_the_full_two_hours_from_four():
    assert decide(night(3, 59, stirred=0.0), Thresholds()).target_speed == 0
    assert decide(night(4, 0, stirred=0.0), Thresholds()).target_speed == 3


def test_a_sunny_day_that_filtered_enough_needs_nothing():
    assert decide(night(5, 30, filtered=130.0), Thresholds()).target_speed == 0


def test_it_runs_on_a_rouge_night_too_the_off_peak_is_cheap():
    assert decide(night(5, 0, filtered=60.0, tempo_color="Rouge"), Thresholds()).target_speed == 1


def test_never_outside_the_off_peak_window():
    assert winter_top_up_due(night(5, 0, is_hc=False), Thresholds()) is None


def test_summer_is_left_as_it_was():
    summer = night(5, 0, season="ete", water_temp_c=25.0, mode="auto")
    assert "complément" not in decide(summer, Thresholds()).reason


def test_minutes_to_the_end_of_the_window_cross_midnight():
    assert minutes_to_hc_end(night(22, 0), Thresholds()) == pytest.approx(480.0)


# ------------------------------------------------------------ the stir
# v1 alone let algae settle in the corners in November 2026: winter also runs
# a while at v2/v3 every day.


def day(hour: int = 13, *, grid: float, speed: int = 1, stirred: float = 0.0,
        **kw) -> Inputs:
    """A winter day, peak hours, the pump at ``speed``."""
    return replace(night(5, 0, stirred=stirred), now=datetime(2026, 12, 10, hour, 0, tzinfo=TZ),
                   daylight=True, is_hc=False, grid_w=grid, pump_speed=speed, **kw)


def test_a_big_surplus_stirs_at_v3():
    decision = decide(day(grid=-1500.0), Thresholds())
    assert (decision.target_speed, decision.enter_v3) == (3, True)


def test_a_smaller_surplus_stirs_at_v2():
    assert decide(day(grid=-800.0), Thresholds()).target_speed == 2


def test_a_surplus_too_small_for_v2_keeps_v1():
    assert decide(day(grid=-500.0), Thresholds()).target_speed == 1


def test_the_stir_holds_through_the_hysteresis():
    """At v3 still exporting a little: stay, do not drop at once."""
    assert decide(day(grid=-100.0, speed=3), Thresholds()).target_speed == 3


def test_once_stirred_the_sun_only_pays_for_v1():
    assert decide(day(grid=-1500.0, stirred=30.0), Thresholds()).target_speed == 1


def test_no_sun_stir_on_a_rouge_day():
    assert decide(day(grid=-1500.0, tempo_color="Rouge"), Thresholds()).target_speed == 0


def test_without_sun_the_night_top_up_stirs_first_at_v3():
    decision = decide(night(4, 0, stirred=0.0), Thresholds())
    assert decision.target_speed == 3 and "brassage" in decision.reason


def test_then_the_top_up_carries_on_at_v1():
    decision = decide(night(4, 30, filtered=30.0, stirred=30.0, pump_speed=3), Thresholds())
    assert (decision.target_speed, decision.leave_v3) == (1, True)


def test_a_day_filtered_on_the_sun_but_never_stirred_stirs_before_six():
    assert decide(night(5, 29, filtered=150.0, stirred=0.0), Thresholds()).target_speed == 0
    assert decide(night(5, 30, filtered=150.0, stirred=0.0), Thresholds()).target_speed == 3


def test_a_partial_sun_stir_is_finished_at_night():
    assert winter_stir_owed(night(5, 0, stirred=20.0), Thresholds()) == 10.0
    assert decide(night(5, 50, filtered=150.0, stirred=20.0), Thresholds()).target_speed == 3


def test_no_stir_when_the_option_is_zero():
    assert decide(day(grid=-1500.0), Thresholds(winter_stir_minutes=0)).target_speed == 1


def test_summer_is_not_stirred():
    summer = day(grid=-1500.0, season="ete", water_temp_c=25.0)
    assert "brassage" not in decide(summer, Thresholds()).reason


# ------------------------------------------------------------ the counting


def test_filtration_counts_only_while_the_pump_runs():
    memory, start = PoolMemory(), datetime(2026, 12, 10, 12, 0, tzinfo=TZ)
    for minute in range(61):
        memory.count_filtration(start + timedelta(minutes=minute), running=minute < 30)
    assert memory.filtered_min == pytest.approx(30.0)


def test_the_filtration_day_starts_at_six():
    memory = PoolMemory()
    memory.count_filtration(datetime(2026, 12, 11, 5, 59, tzinfo=TZ), running=True)
    memory.count_filtration(datetime(2026, 12, 11, 6, 0, tzinfo=TZ), running=True)
    assert (memory.filtered_min, memory.filtration_day) == (0.0, date(2026, 12, 11))


def test_only_v2_and_v3_count_as_stirring():
    memory, start = PoolMemory(), datetime(2026, 12, 10, 12, 0, tzinfo=TZ)
    for minute in range(61):
        memory.count_filtration(start + timedelta(minutes=minute), running=True,
                                stirring=minute < 20)
    assert (memory.filtered_min, memory.stirred_min) == pytest.approx((60.0, 20.0))


def test_the_stir_resets_at_six_too():
    memory = PoolMemory()
    memory.count_filtration(datetime(2026, 12, 11, 5, 0, tzinfo=TZ), running=True, stirring=True)
    memory.count_filtration(datetime(2026, 12, 11, 5, 10, tzinfo=TZ), running=True, stirring=True)
    memory.count_filtration(datetime(2026, 12, 11, 6, 0, tzinfo=TZ), running=True)
    assert memory.stirred_min == 0.0


def test_a_long_gap_is_not_counted_whole():
    memory = PoolMemory()
    memory.count_filtration(datetime(2026, 12, 10, 12, 0, tzinfo=TZ), running=True)
    memory.count_filtration(datetime(2026, 12, 10, 14, 0, tzinfo=TZ), running=True)
    assert memory.filtered_min == 10.0


def test_no_top_up_when_the_option_is_zero():
    assert winter_top_up_due(night(5, 30), Thresholds(winter_daily_minutes=0)) is None


def test_forced_summer_ignores_cold_water():
    day = replace(night(5, 0), now=datetime(2026, 12, 10, 14, 0, tzinfo=TZ), daylight=True,
                  is_hc=False, mode="summer", grid_w=-600.0)
    decision = decide(day, Thresholds())
    assert decision.season == "ete" and "winter" not in decision.reason
