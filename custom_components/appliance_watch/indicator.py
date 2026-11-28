"""Home Assistant side of the indicator lamp (:mod:`logic.indicator`).

Listens to the grid meter, the panels, the lamp and the other lights of its
room, and sends the lamp its colour and brightness at every reading — a few
seconds behind the meter, not on the coordinator's 30 s tick. The grid and the
panels are smoothed over ``NET_TAU_S``/``PV_TAU_S``, short enough to follow a
kettle, long enough not to chase every flicker of the meter.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GRID,
    CONF_INDICATOR_IGNORE,
    CONF_INDICATOR_LIGHT,
    CONF_INDICATOR_MIN_PV,
    CONF_INDICATOR_ROOM,
    CONF_SOLAR,
    DOMAIN,
    STORAGE_VERSION,
    entry_value,
)
from .logic.indicator import DEFAULT_MIN_PV_W, Command, Inputs, Memory, decide
from .logic.residual import to_watts
from .logic.router_state import smooth

_LOGGER = logging.getLogger(__name__)

NET_TAU_S = 8.0
PV_TAU_S = 30.0
UNAVAILABLE = ("unknown", "unavailable", "")
FADE_S = 2  # each update fades over the minimum interval between two


class Indicator:
    """One lamp showing the house's draw while the sun shines and its room is dark."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.voyant")
        self.memory = Memory()
        self.enabled = True
        self.net_w: float | None = None
        self.pv_w: float | None = None
        self._read_at: datetime | None = None
        self._unsubscribe: list[Any] = []

    # ------------------------------------------------------------ settings

    def _entity(self, key: str) -> str | None:
        return entry_value(self.entry, key) or None

    @property
    def lamp(self) -> str | None:
        return self._entity(CONF_INDICATOR_LIGHT)

    @property
    def configured(self) -> bool:
        return all(self._entity(key) for key in (CONF_INDICATOR_LIGHT, CONF_GRID, CONF_SOLAR))

    def _others(self) -> list[str]:
        """The room group's members (a Hue room lists them), less the lamp and
        the lights that do not count as lighting the room."""
        room = self._entity(CONF_INDICATOR_ROOM)
        state = self.hass.states.get(room) if room else None
        members = state.attributes.get("entity_id", []) if state else []
        ignored = {self.lamp, *(entry_value(self.entry, CONF_INDICATOR_IGNORE, []) or [])}
        return [member for member in members if member not in ignored]

    # ----------------------------------------------------------- lifecycle

    async def async_setup(self) -> None:
        if not self.configured:
            return
        data = await self._store.async_load() or {}
        self.enabled = data.get("enabled", True)
        self.memory.owned = data.get("owned", False)
        watched = [self._entity(CONF_GRID), self._entity(CONF_SOLAR), self.lamp,
                   self._entity(CONF_INDICATOR_ROOM), *self._others()]
        self._unsubscribe.append(async_track_state_change_event(
            self.hass, [entity for entity in watched if entity], self._on_change))

    def teardown(self) -> None:
        while self._unsubscribe:
            self._unsubscribe.pop()()

    async def async_set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        await self._save()
        await self.async_evaluate(dt_util.utcnow())

    async def _save(self) -> None:
        await self._store.async_save({"enabled": self.enabled, "owned": self.memory.owned})

    # ------------------------------------------------------------- reading

    def _watts(self, key: str) -> float | None:
        entity = self._entity(key)
        state = self.hass.states.get(entity) if entity else None
        if state is None or state.state in UNAVAILABLE:
            return None
        try:
            return to_watts(float(state.state), state.attributes.get("unit_of_measurement"))
        except ValueError:
            return None

    def _feed(self, now: datetime) -> None:
        elapsed = (now - self._read_at).total_seconds() if self._read_at else 0.0
        grid, pv = self._watts(CONF_GRID), self._watts(CONF_SOLAR)
        self.net_w = None if grid is None else smooth(self.net_w, grid, elapsed, NET_TAU_S)
        self.pv_w = None if pv is None else smooth(self.pv_w, max(pv, 0.0), elapsed, PV_TAU_S)
        self._read_at = now

    def _lamp_on(self) -> bool | None:
        state = self.hass.states.get(self.lamp) if self.lamp else None
        return None if state is None or state.state in UNAVAILABLE else state.state == "on"

    # ------------------------------------------------------------ deciding

    @callback
    def _on_change(self, event: Event[EventStateChangedData]) -> None:
        self.hass.async_create_task(self.async_evaluate(dt_util.utcnow()))

    async def async_evaluate(self, now: datetime) -> None:
        self._feed(now)
        owned = self.memory.owned
        command = decide(Inputs(
            now=now, net_w=self.net_w, pv_w=self.pv_w, lamp_on=self._lamp_on(),
            others_on=any(self.hass.states.is_state(e, "on") for e in self._others()),
            enabled=self.enabled,
            min_pv_w=float(entry_value(self.entry, CONF_INDICATOR_MIN_PV, DEFAULT_MIN_PV_W)),
        ), self.memory)
        if command is not None:
            await self._send(command)
        if self.memory.owned != owned:
            await self._save()

    async def _send(self, command: Command) -> None:
        data: dict[str, Any] = {"entity_id": self.lamp, "transition": FADE_S}
        if command.action == "on":
            data |= {"hs_color": [command.hue, 100], "brightness": command.brightness}
        try:
            await self.hass.services.async_call("light", f"turn_{command.action}", data,
                                                blocking=False)
        except HomeAssistantError as err:
            _LOGGER.warning("Voyant : light.turn_%s refusé : %s", command.action, err)
