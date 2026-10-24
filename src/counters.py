"""
Traffic counter and rate calculation logic.

Synthetic traffic is represented through standard SNMP byte counters. Counter
increments are derived from actual elapsed wall-clock time and a resolved traffic
profile; callers do not expose an instantaneous synthetic rate as an SNMP value.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class TrafficCounter:
    """One interface's Counter32/Counter64 byte-counter state."""

    INCREMENT_WIDTH_32 = 2 ** 32
    INCREMENT_WIDTH_64 = 2 ** 64

    link_speed_mbps: int = 1000
    rate_min_mbps: float = 0
    rate_max_mbps: float = 10
    variation_percent: float = 10
    seed: int | None = None

    _base_32: int = field(default=0, init=False)
    _base_64: int = field(default=0, init=False)
    _last_update: float = field(default=0.0, init=False)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._last_update = time.time()
        # A deterministic seed produces deterministic initial generated state.
        # Unseeded standalone counters retain pseudo-random initial state.
        self._base_64 = self._rng.randint(0, self.INCREMENT_WIDTH_64 // 100)
        self._base_32 = self._base_64 % self.INCREMENT_WIDTH_32

    def get_rates(self) -> tuple[float, float]:
        """Return the current synthetic in/out rate in Mbps.

        A base value is selected from the configured range, then the configured
        variation is applied.  The final rate is clamped to the physical link
        speed and never allowed below zero.
        """
        low = max(0.0, float(self.rate_min_mbps))
        high = max(low, float(self.rate_max_mbps))
        base = low if high == low else self._rng.uniform(low, high)
        variation = self._rng.uniform(-self.variation_percent, self.variation_percent) / 100.0
        current = base * (1.0 + variation)
        current = min(max(0.0, current), float(self.link_speed_mbps))
        return current, current

    def update(self, now: float | None = None) -> dict:
        """Advance counters according to actual elapsed time."""
        if now is None:
            now = time.time()

        elapsed = max(0.0, now - self._last_update)
        rate_mbps, _ = self.get_rates()
        bytes_to_add = int((rate_mbps * 1_000_000 / 8.0) * elapsed)

        self._base_64 = (self._base_64 + bytes_to_add) % self.INCREMENT_WIDTH_64
        self._base_32 = self._base_64 % self.INCREMENT_WIDTH_32
        self._last_update = now
        return self.get_values()

    def get_values(self) -> dict:
        return {
            "ifInOctets_32": self._base_32,
            "ifOutOctets_32": self._base_32,
            "ifHCInOctets_64": self._base_64,
            "ifHCOutOctets_64": self._base_64,
        }

    def reset(self, now: float | None = None) -> None:
        """Reset byte counters once after a simulated power recovery."""
        self._base_32 = 0
        self._base_64 = 0
        self._last_update = time.time() if now is None else now


class TrafficProfile:
    """Resolved traffic profile for a device with one or more interfaces.

    Canonical runtime profile fields are:
      interfaces, link_speed_mbps, default_rate_mbps, variation_percent,
      periods, timezone, random_seed.

    ``timezone`` is normally an IANA zone such as ``Europe/Copenhagen``.
    Numeric minute offsets are retained only for backwards compatibility with
    early unit fixtures.
    """

    def __init__(self, profile: dict):
        self.profile = profile
        self.num_interfaces = int(profile.get("interfaces", 1))
        self.timezone_name = profile.get("timezone", "UTC")
        self.periods = list(profile.get("periods", []))
        self.random_seed = profile.get("random_seed")

        if isinstance(self.timezone_name, str):
            try:
                self._timezone = ZoneInfo(self.timezone_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"Unknown traffic-profile timezone: {self.timezone_name!r}") from exc
            self._timezone_offset_minutes = None
        else:
            self._timezone = None
            self._timezone_offset_minutes = int(self.timezone_name)

        defaults = profile.get("default_rate_mbps", {})
        self.counters: list[TrafficCounter] = []
        for idx in range(self.num_interfaces):
            seed = None if self.random_seed is None else int(self.random_seed) + idx
            self.counters.append(TrafficCounter(
                link_speed_mbps=int(profile.get("link_speed_mbps", 1000)),
                rate_min_mbps=float(defaults.get("min", 0)),
                rate_max_mbps=float(defaults.get("max", 10)),
                variation_percent=float(profile.get("variation_percent", 10)),
                seed=seed,
            ))

    @staticmethod
    def _period_rate(period: dict) -> tuple[float, float]:
        """Read canonical period rate keys, accepting the early *_mbps form."""
        return (
            float(period.get("rate_min", period.get("rate_min_mbps", 0))),
            float(period.get("rate_max", period.get("rate_max_mbps", 10))),
        )

    def _local_minute_of_day(self, now: float) -> int:
        if self._timezone is not None:
            local = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(self._timezone)
            return local.hour * 60 + local.minute
        shifted = now + (self._timezone_offset_minutes * 60)
        utc = datetime.fromtimestamp(shifted, tz=timezone.utc)
        return utc.hour * 60 + utc.minute

    def get_current_rates(self, now: float | None = None) -> tuple[float, float]:
        """Select the configured rate range for the local time of day."""
        if now is None:
            now = time.time()

        default = self.profile.get("default_rate_mbps", {})
        default_rates = (float(default.get("min", 0)), float(default.get("max", 10)))
        if not self.periods:
            return default_rates

        minute = self._local_minute_of_day(now)
        for period in self.periods:
            start = int(period.get("start_hour", 0)) * 60 + int(period.get("start_min", 0))
            end = int(period.get("end_hour", 0)) * 60 + int(period.get("end_min", 0))

            # Equal endpoints mean a full-day override.
            if start == end:
                return self._period_rate(period)
            if start < end:
                matches = start <= minute < end
            else:
                matches = minute >= start or minute < end
            if matches:
                return self._period_rate(period)

        return default_rates

    def update_all(self, now: float | None = None) -> list[dict]:
        if now is None:
            now = time.time()
        rate_min, rate_max = self.get_current_rates(now)

        results = []
        for counter in self.counters:
            original_min = counter.rate_min_mbps
            original_max = counter.rate_max_mbps
            try:
                counter.rate_min_mbps = rate_min
                counter.rate_max_mbps = rate_max
                results.append(counter.update(now))
            finally:
                counter.rate_min_mbps = original_min
                counter.rate_max_mbps = original_max
        return results

    def reset_all(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        for counter in self.counters:
            counter.reset(now)
