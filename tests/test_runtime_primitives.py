from src.counters import TrafficCounter
from src.scheduler import DeviceState, OutageProfile, OutageScheduler
from src.uptime import MAX_TIMEBITS, TICKS_PER_SECOND, UptimeTracker


def test_counter64_grows_with_elapsed_time():
    counter = TrafficCounter(
        link_speed_mbps=100,
        rate_min_mbps=8,
        rate_max_mbps=8,
        variation_percent=0,
        seed=1,
    )
    before = counter.get_values()["ifHCInOctets_64"]
    now = counter._last_update + 1.0
    after = counter.update(now)["ifHCInOctets_64"]
    assert after > before
    assert after - before == 1_000_000  # 8 Mbit/s for one second = 1,000,000 bytes


def test_uptime_wraps_at_timeticks_32bit_boundary():
    clock = [1000.0]
    tracker = UptimeTracker(0, clock=lambda: clock[0])
    tracker.set_initial_uptime(MAX_TIMEBITS // TICKS_PER_SECOND)
    # Initial uptime is stored in whole seconds, so advance the remaining
    # 0.96 seconds to reach the exact 2^32 TimeTicks wrap boundary.
    clock[0] += (MAX_TIMEBITS % TICKS_PER_SECOND) / TICKS_PER_SECOND
    assert tracker.get_uptime_ticks() == 0
    clock[0] += 1.0
    assert tracker.get_uptime_ticks() == 100


def test_uptime_reset_sets_visible_uptime_back_to_zero():
    clock = [1000.0]
    tracker = UptimeTracker(10_000, clock=lambda: clock[0])
    clock[0] += 50
    assert tracker.get_uptime_ticks() > 0
    tracker.reset()
    assert tracker.get_uptime_ticks() == 0


class FixedRng:
    def expovariate(self, _rate):
        return 1.0  # minute until outage

    def uniform(self, low, _high):
        return low  # minimum outage duration


def test_outage_scheduler_cycles_offline_then_online():
    profile = OutageProfile(
        enabled=True,
        expected_events_per_day=5,
        duration_min_minutes=1,
        duration_max_minutes=1,
        minimum_uptime_minutes=0,
    )
    scheduler = OutageScheduler(profile, seed=1, rng=FixedRng())
    scheduler.set_initial_time(0)
    assert scheduler.advance(60) == ["power_off"]
    assert scheduler.state == DeviceState.OFFLINE
    assert "power_on" in scheduler.advance(120)
    assert scheduler.state == DeviceState.ONLINE
