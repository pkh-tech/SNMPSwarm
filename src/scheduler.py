"""
Outage scheduler logic.

Event-driven outage model:
    ONLINE -> OFFLINE -> ONLINE

Supports injected clock and random sources for deterministic simulation.
"""

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DeviceState(Enum):
    ONLINE = "online"
    OFFLINE = "offline"


@dataclass
class OutageEvent:
    """A scheduled outage event."""
    start_time: float
    end_time: float


@dataclass
class OutageProfile:
    """Configuration for outage scheduling."""
    enabled: bool
    expected_events_per_day: float
    duration_min_minutes: float
    duration_max_minutes: float
    minimum_uptime_minutes: float


class OutageScheduler:
    """Manages the online/offline state of a single device.

    Uses an exponential inter-arrival distribution for outage events.
    """

    def __init__(
        self,
        profile: OutageProfile,
        seed: int,
        clock=None,
        rng=None,
    ):
        self.profile = profile
        self.clock = clock or (lambda: 0.0)
        self.rng = rng if rng is not None else random.Random(seed)
        self.state = DeviceState.ONLINE
        self._next_outage_time: Optional[float] = None
        self._current_outage: Optional[OutageEvent] = None

    @property
    def is_online(self) -> bool:
        return self.state == DeviceState.ONLINE

    def set_initial_time(self, t: float | None = None):
        """Set the initial simulation time.

        Schedules the first potential outage event.
        """
        if t is None:
            t = self.clock()
        self._current_time = t
        if self.profile.enabled and self.profile.expected_events_per_day > 0:
            self._schedule_next_outage(t)

    def _schedule_next_outage(self, from_time: float):
        """Schedule the next outage event from the given time.

        Uses exponential distribution for inter-arrival times.
        from_time is in seconds. Internal calculations in minutes.
        """
        rate = self.profile.expected_events_per_day
        if rate <= 0:
            self._next_outage_time = float("inf")
            return

        # Mean interval in minutes
        mean_interval_minutes = (1440.0 / rate)  # 1440 minutes per day
        # Exponential distribution
        interval_minutes = self.rng.expovariate(1.0 / mean_interval_minutes)
        # Ensure minimum uptime is respected
        min_uptime = self.profile.minimum_uptime_minutes
        interval_minutes = max(interval_minutes, min_uptime)

        # Convert to seconds for consistency with simulation time (seconds)
        self._next_outage_time = from_time + (interval_minutes * 60.0)

    def advance(self, to_time: float) -> list[str]:
        """Advance the scheduler to the given time.

        Returns a list of events that occurred:
            "power_off" / "power_on"
        """
        events = []

        if not self.profile.enabled or self._next_outage_time is None:
            return events

        if to_time < self._next_outage_time and self.state == DeviceState.ONLINE:
            # No outage yet
            return events

        # Process all outage events up to to_time
        max_iterations = 10000  # Prevent infinite loop
        iterations = 0
        while self._next_outage_time is not None and iterations < max_iterations:
            iterations += 1
            
            if self.state == DeviceState.ONLINE and to_time < self._next_outage_time:
                # No outage yet within the time range
                break

            if self.state == DeviceState.ONLINE:
                # Schedule outage start
                outage_start = self._next_outage_time
                # Convert outage duration from minutes to seconds
                outage_duration_seconds = self.rng.uniform(
                    self.profile.duration_min_minutes * 60.0,
                    self.profile.duration_max_minutes * 60.0,
                )
                outage_end = outage_start + outage_duration_seconds

                # Trigger power off
                self.state = DeviceState.OFFLINE
                self._current_outage = OutageEvent(outage_start, outage_end)
                events.append("power_off")

                # Advance past outage
                self._next_outage_time = outage_end
                if to_time >= outage_end:
                    # Trigger power on immediately (outage fits within the step)
                    self.state = DeviceState.ONLINE
                    events.append("power_on")
                    # Schedule next outage
                    self._schedule_next_outage(outage_end)
                # If outage hasn't ended yet, we'll handle power-on when to_time reaches outage_end
            elif self.state == DeviceState.OFFLINE and self._current_outage:
                # Check if outage has ended
                outage_end = self._current_outage.end_time
                if to_time >= outage_end:
                    # Trigger power on
                    self.state = DeviceState.ONLINE
                    events.append("power_on")
                    # Schedule next outage
                    self._schedule_next_outage(outage_end)
                else:
                    # Outage still ongoing, stop advancing
                    break

        return events

    def advance_one(self, to_time: float) -> list[str]:
        """Advance to time with one event check."""
        return self.advance(to_time)

    def get_state(self, at_time: float) -> tuple[DeviceState, Optional[OutageEvent]]:
        """Get device state at a specific time."""
        if self.state == DeviceState.OFFLINE and self._current_outage:
            if at_time >= self._current_outage.end_time and self._next_outage_time is not None:
                # Already powered back on
                return DeviceState.ONLINE, None

        return self.state, self._current_outage

    def simulate_full_cycle(self, start_time: float, end_time: float, step: float = 1.0) -> list[str]:
        """Simulate from start_time to end_time with given step size.

        Returns list of events.
        """
        all_events = []
        t = start_time
        while t <= end_time:
            events = self.advance(t)
            all_events.extend(events)
            # Also check if an ongoing outage ends during this step
            if self.state == DeviceState.OFFLINE and self._current_outage:
                outage_end = self._current_outage.end_time
                if t >= outage_end:
                    # Power on occurred but wasn't captured
                    if "power_on" not in events:
                        self.state = DeviceState.ONLINE
                        events.append("power_on")
                        all_events.append("power_on")
                        self._schedule_next_outage(outage_end)
            t += step
        return all_events
