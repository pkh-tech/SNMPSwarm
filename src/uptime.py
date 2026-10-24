"""
Uptime behavior with TimeTicks wrap.

SNMP sysUpTime is a TimeTicks value (hundredths of a second),
32-bit, wrapping at 2^32 = 4294967296 hundredths = ~497 days.

Internal uptime is tracked as 64-bit but exposed wrapped.
"""

from __future__ import annotations


MAX_TIMEBITS = 2 ** 32  # 32-bit wrap value (hundredths of a second)
TICKS_PER_SECOND = 100


class UptimeTracker:
    """Tracks internal uptime and provides wrapped exposure."""

    def __init__(self, initial_seconds: int, clock: callable | None = None):
        """
        Args:
            initial_seconds: Initial uptime in seconds.
            clock: Optional callable returning current time in seconds.
                   Defaults to time.time().
        """
        import time as _time
        self._clock = clock or _time.time
        self._internal_uptime = initial_seconds  # Internal 64-bit uptime in seconds
        self._boot_time = self._clock()  # When this uptime was set
        self._power_cycled = False

    def get_uptime_seconds(self) -> float:
        """Get current uptime in seconds (with wrap).

        Returns:
            Uptime as hundredths of a second, wrapped at 2^32.
        """
        elapsed = (self._clock() - self._boot_time) * TICKS_PER_SECOND
        raw_uptime = (self._internal_uptime * TICKS_PER_SECOND) + elapsed
        # Wrap at 32-bit
        wrapped = raw_uptime % MAX_TIMEBITS
        return wrapped

    def get_uptime_ticks(self) -> int:
        """Get sysUpTime.0 value as integer hundredths of a second (32-bit)."""
        return int(self.get_uptime_seconds())

    def get_uptime_seconds_raw(self) -> float:
        """Get raw internal uptime in seconds (64-bit, unwrapped)."""
        elapsed = self._clock() - self._boot_time
        return self._internal_uptime + elapsed

    def reset(self):
        """Simulate power cycle: reset uptime to zero."""
        self._boot_time = self._clock()
        self._internal_uptime = 0

    def set_initial_uptime(self, seconds: int):
        """Set initial uptime (call only at boot)."""
        self._internal_uptime = seconds
        self._boot_time = self._clock()


def time_ticks_to_days(ticks: int) -> float:
    """Convert TimeTicks (hundredths of a second) to days."""
    return ticks / (TICKS_PER_SECOND * 3600 * 24)


def seconds_to_ticks(seconds: int) -> int:
    """Convert seconds to hundredths of a second."""
    return seconds * TICKS_PER_SECOND
