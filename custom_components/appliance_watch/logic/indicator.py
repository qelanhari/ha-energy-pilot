"""The indicator lamp: the house's net draw as a colour, the panels as brightness.

A lamp lights up on its own while the panels make at least ``min_pv_w`` and
nothing else in its room is on. Its colour sweeps the hue circle one way,
fully saturated — green as soon as the house exports (lime for a little,
deep green from 1.5 kW), then from 0 W cyan,
blue at 700 W, violet, magenta and red from 6 kW — so no colour ever means
two things. Its
brightness follows the panels. Pure: the Home Assistant side reads and sends.

The lamp is ours only once we lit it. Turned off by hand, it stays off until
the conditions lapse (the sun goes, or the room is lit) and come back. When
the room is lit, the lamp is left to the room — its scene sets it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Literal

# Export: green throughout, from lime (a little) to pure green (EXPORT_FULL_W).
# Yellow is used nowhere else on the scale.
EXPORT_HUE_SMALL = 80.0
EXPORT_HUE = 120.0
EXPORT_FULL_W = 1500.0
# (net W, hue in degrees) from 0 W, increasing both; constant beyond the last.
# Sent as hs_color at full saturation: the lamp's whole gamut, never a greyish mix.
STOPS: tuple[tuple[float, float], ...] = (
    (0.0, 180.0),  # cyan
    (700.0, 235.0),  # blue: the house's ordinary draw
    (1500.0, 270.0),  # violet
    (3000.0, 300.0),  # magenta
    (6000.0, 360.0),  # red, and beyond
)
PV_FULL_W = 5000.0  # full brightness from here
MIN_BRIGHTNESS = 40  # of 255, at min_pv_w
PV_OFF_MARGIN_W = 50.0  # lit at min_pv_w, out below min_pv_w - margin: no dusk flicker
MIN_INTERVAL_S = 2.0  # between two colour updates; the Hue bridge copes easily
HUE_STEP = 2.0  # degrees the hue must move to be worth a command
BRIGHTNESS_STEP = 4
DEFAULT_MIN_PV_W = 200.0


def hue(net_w: float) -> float:
    """Green on export; from 0 W, linear between two stops. Red is 0, not 360."""
    if net_w < 0:
        t = min(-net_w / EXPORT_FULL_W, 1.0)
        return round(EXPORT_HUE_SMALL + (EXPORT_HUE - EXPORT_HUE_SMALL) * t, 1)
    for (low_w, low), (high_w, high) in pairwise(STOPS):
        if net_w <= high_w:
            return round((low + (high - low) * (net_w - low_w) / (high_w - low_w)) % 360, 1)
    return STOPS[-1][1] % 360


def brightness(pv_w: float, min_pv_w: float) -> int:
    """MIN_BRIGHTNESS at ``min_pv_w``, 255 from PV_FULL_W, linear between."""
    span = max(PV_FULL_W - min_pv_w, 1.0)
    t = min(max((pv_w - min_pv_w) / span, 0.0), 1.0)
    return round(MIN_BRIGHTNESS + (255 - MIN_BRIGHTNESS) * t)


@dataclass(frozen=True)
class Inputs:
    now: datetime
    net_w: float | None  # smoothed, > 0 import
    pv_w: float | None  # smoothed
    lamp_on: bool | None  # None: unavailable
    others_on: bool  # any other light of the room
    enabled: bool
    min_pv_w: float = DEFAULT_MIN_PV_W


@dataclass(frozen=True)
class Command:
    action: Literal["on", "off"]
    reason: str
    hue: float | None = None
    brightness: int | None = None


@dataclass
class Memory:
    owned: bool = False  # we lit it and nobody took it back
    seen_on: bool = False  # it did come on after we lit it
    suppressed: bool = False  # turned off by hand: wait for the conditions to lapse
    sunny: bool = False  # the panel hysteresis
    sent_hue: float | None = None
    sent_brightness: int | None = None
    sent_at: datetime | None = None
    reason: str = ""

    def forget_sent(self) -> None:
        self.sent_hue = self.sent_brightness = self.sent_at = None


def _is_sunny(i: Inputs, m: Memory) -> bool:
    assert i.pv_w is not None
    floor = i.min_pv_w - PV_OFF_MARGIN_W if m.sunny else i.min_pv_w
    return i.pv_w >= floor


def _worth_sending(i: Inputs, m: Memory, colour: float, level: int) -> bool:
    if m.sent_hue is None or m.sent_brightness is None or m.sent_at is None:
        return True
    if (i.now - m.sent_at).total_seconds() < MIN_INTERVAL_S:
        return False
    moved = abs(colour - m.sent_hue)
    moved = min(moved, 360 - moved)
    return moved >= HUE_STEP or abs(level - m.sent_brightness) >= BRIGHTNESS_STEP


def _release(i: Inputs, m: Memory, why: str) -> Command | None:
    """Conditions lapsed: hand the lamp back, off unless the room took it."""
    m.suppressed = False
    m.reason = why
    if not m.owned:
        return None
    m.owned = m.seen_on = False
    m.forget_sent()
    if i.others_on or not i.lamp_on:
        return None
    return Command("off", why)


def decide(i: Inputs, m: Memory) -> Command | None:
    """What to send now, if anything. Updates ``m``."""
    if i.net_w is None or i.pv_w is None or i.lamp_on is None:
        m.reason = "mesure manquante"
        return None
    if m.owned and i.lamp_on:
        m.seen_on = True
    elif m.owned and m.seen_on:
        m.owned = m.seen_on = False
        m.suppressed = True
        m.forget_sent()
    m.sunny = _is_sunny(i, m)
    if not i.enabled:
        return _release(i, m, "voyant désactivé")
    if i.others_on:
        return _release(i, m, "pièce allumée")
    if not m.sunny:
        return _release(i, m, f"solaire < {i.min_pv_w:.0f} W")
    if m.suppressed:
        m.reason = "éteint à la main"
        return None
    colour, level = hue(i.net_w), brightness(i.pv_w, i.min_pv_w)
    m.reason = f"réseau {i.net_w:.0f} W · solaire {i.pv_w:.0f} W"
    if m.owned and not _worth_sending(i, m, colour, level):
        return None
    if not m.owned:
        m.owned, m.seen_on = True, bool(i.lamp_on)
    m.sent_hue, m.sent_brightness, m.sent_at = colour, level, i.now
    return Command("on", m.reason, colour, level)
