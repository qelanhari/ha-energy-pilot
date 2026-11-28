"""The indicator lamp: colour of the net draw, brightness of the sun."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "appliance_watch"))

from logic.indicator import (
    MIN_BRIGHTNESS,
    Inputs,
    Memory,
    brightness,
    decide,
    hue,
)

T0 = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def inputs(**kw) -> Inputs:
    base = Inputs(now=T0, net_w=800.0, pv_w=1500.0, lamp_on=False, others_on=False,
                  enabled=True)
    return replace(base, **kw)


# ------------------------------------------------------------ the scale


@pytest.mark.parametrize("watts, expected", [
    (-3000, 120), (-1500, 120), (0, 180), (700, 235), (1500, 270), (3000, 300),
    (6000, 0), (20000, 0)])
def test_the_scale_green_cyan_blue_violet_magenta_red(watts, expected):
    assert hue(watts) == expected


def test_green_as_soon_as_the_house_exports():
    assert 80 <= hue(-0.5) < 81 and hue(-5000) == 120


def test_a_little_export_is_lime_a_lot_is_deep_green():
    assert hue(-200) == pytest.approx(85.3)
    assert hue(-1000) == pytest.approx(106.7)
    assert hue(-200) < hue(-1000) < hue(-1500)


def test_blue_only_from_seven_hundred_watts():
    assert hue(350) == pytest.approx(207.5)


def test_more_watts_always_further_round_the_circle():
    hues = [hue(w) or 360 for w in range(0, 6001, 100)]
    assert hues == sorted(hues) and len(set(hues)) == len(hues)


def test_brightness_follows_the_panels():
    assert brightness(200, 200) == MIN_BRIGHTNESS
    assert brightness(5000, 200) == 255
    assert brightness(9000, 200) == 255
    assert MIN_BRIGHTNESS < brightness(2600, 200) < 255


# ------------------------------------------------------------ lighting up


def test_the_sun_lights_it_in_the_colour_of_the_draw():
    command = decide(inputs(), Memory())
    assert command is not None and command.action == "on"
    assert command.hue == hue(800) and command.brightness == brightness(1500, 200)


def test_not_below_the_minimum_production():
    assert decide(inputs(pv_w=199.0), Memory()) is None


def test_not_while_the_room_is_lit():
    assert decide(inputs(others_on=True), Memory()) is None


def test_not_when_disabled():
    assert decide(inputs(enabled=False), Memory()) is None


def test_holds_on_a_missing_reading():
    memory = Memory()
    assert decide(inputs(net_w=None), memory) is None
    assert decide(inputs(lamp_on=None), memory) is None


# ------------------------------------------------------------ following


def lit() -> Memory:
    memory = Memory()
    decide(inputs(), memory)
    decide(inputs(lamp_on=True, now=T0 + timedelta(seconds=1)), memory)
    return memory


def test_a_new_colour_follows_the_draw():
    command = decide(inputs(lamp_on=True, net_w=5000.0, now=T0 + timedelta(seconds=3)), lit())
    assert command is not None and command.hue == hue(5000)


def test_noise_sends_nothing():
    assert decide(inputs(lamp_on=True, net_w=810.0, now=T0 + timedelta(seconds=3)),
                  lit()) is None


def test_no_more_than_one_update_every_two_seconds():
    memory = lit()
    assert decide(inputs(lamp_on=True, net_w=5000.0, now=T0 + timedelta(seconds=1.5)),
                  memory) is None


# ------------------------------------------------------------ letting go


def test_the_sun_going_turns_it_off():
    command = decide(inputs(lamp_on=True, pv_w=100.0, now=T0 + timedelta(seconds=3)), lit())
    assert command is not None and command.action == "off"


def test_no_dusk_flicker_just_under_the_minimum():
    command = decide(inputs(lamp_on=True, pv_w=170.0, now=T0 + timedelta(seconds=3)), lit())
    assert command is not None and command.action == "on"
    assert command.brightness == MIN_BRIGHTNESS


def test_the_room_lit_takes_the_lamp_over_without_turning_it_off():
    memory = lit()
    assert decide(inputs(lamp_on=True, others_on=True), memory) is None
    assert not memory.owned


def test_disabling_turns_it_off():
    command = decide(inputs(lamp_on=True, enabled=False), lit())
    assert command is not None and command.action == "off"


def test_turned_off_by_hand_it_stays_off():
    memory = lit()
    assert decide(inputs(lamp_on=False, now=T0 + timedelta(seconds=5)), memory) is None
    assert decide(inputs(lamp_on=False, now=T0 + timedelta(minutes=5)), memory) is None
    assert memory.suppressed


def test_until_the_conditions_lapse_and_come_back():
    memory = lit()
    decide(inputs(lamp_on=False, now=T0 + timedelta(seconds=5)), memory)
    decide(inputs(lamp_on=False, others_on=True), memory)
    command = decide(inputs(lamp_on=False, now=T0 + timedelta(minutes=10)), memory)
    assert command is not None and command.action == "on"


def test_a_lamp_slow_to_report_on_is_not_taken_as_turned_off():
    memory = Memory()
    decide(inputs(), memory)
    decide(inputs(lamp_on=False, now=T0 + timedelta(seconds=1)), memory)
    assert memory.owned and not memory.suppressed


def test_a_lamp_we_never_lit_is_never_turned_off():
    assert decide(inputs(lamp_on=True, pv_w=0.0), Memory()) is None
