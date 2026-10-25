#!/usr/bin/env python3
"""
SNMP agent and power-state manager.

Runs inside each container as PID 1. Handles:
- SNMPv2c on UDP:161 (pure Python, no external SNMP library)
- ICMP echo via kernel (not userspace)
- Power state (online/offline) via the HTTP control API on port 8080
  (GET /status, POST /power/on, POST /power/off); SIGTERM/SIGINT are for
  graceful process termination only
- Uptime with TimeTicks wrap (via src.uptime.UptimeTracker)
- Traffic counters with correct modulo behavior (via src.counters)
- Outage scheduling (via src.scheduler.OutageScheduler)

Uses shared runtime logic from src/.
No external SNMP libraries needed - all SNMP encoding/decoding is manual.
"""

import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

# Add the parent directory to sys.path so we can import src modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.uptime import UptimeTracker, MAX_TIMEBITS, TICKS_PER_SECOND
from src.counters import TrafficCounter, TrafficProfile
from src.scheduler import OutageScheduler, OutageProfile, DeviceState


def log_event(simulation_name: str, device_id: int | str, message: str) -> None:
    """Emit one structured agent event."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(
        f"{timestamp} [{simulation_name}] [Device {device_id}] {message}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# SNMP Data Store
# ---------------------------------------------------------------------------

class SNMPDataStore:
    """Stores all SNMP data for one device.

    Uses UptimeTracker from src/uptime, TrafficCounter from src/counters,
    and OutageScheduler from src/scheduler.
    """

    # System group OIDs (RFC 2578)
    SYS_DESCR_OID = (1, 3, 6, 1, 2, 1, 1, 1, 0)
    SYS_OBJECT_ID_OID = (1, 3, 6, 1, 2, 1, 1, 2, 0)
    SYS_UP_TIME_OID = (1, 3, 6, 1, 2, 1, 1, 3, 0)
    SYS_CONTACT_OID = (1, 3, 6, 1, 2, 1, 1, 4, 0)
    SYS_NAME_OID = (1, 3, 6, 1, 2, 1, 1, 5, 0)
    SYS_LOCATION_OID = (1, 3, 6, 1, 2, 1, 1, 6, 0)
    SYS_SERVICES_OID = (1, 3, 6, 1, 2, 1, 1, 7, 0)

    IF_NUMBER_OID = (1, 3, 6, 1, 2, 1, 2, 1, 0)
    IF_INDEX_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 1)
    IF_DESCR_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 2)
    IF_TYPE_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 3)
    IF_MTU_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 4)
    IF_SPEED_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 5)
    IF_PHYS_ADDRESS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 6)
    IF_ADMIN_STATUS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 7)
    IF_OPER_STATUS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 8)
    IF_IN_OCTETS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 10)
    IF_OUT_OCTETS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 16)
    IF_IN_ERRORS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 14)
    IF_OUT_ERRORS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 20)
    IF_IN_DISCARDS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 13)
    IF_OUT_DISCARDS_OID_BASE = (1, 3, 6, 1, 2, 1, 2, 2, 1, 19)
    # IF-MIB::ifName (1.3.6.1.2.1.31.1.1.1.1)
    IF_NAME_OID_BASE = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 1)
    # IF-MIB::ifHighSpeed (1.3.6.1.2.1.31.1.1.1.15)
    IF_HIGH_SPEED_OID_BASE = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 15)
    IF_HC_IN_OCTETS_OID_BASE = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 6)
    IF_HC_OUT_OCTETS_OID_BASE = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 10)

    def __init__(self, config: dict):
        self.device_id = config["device_id"]
        self.simulation_name = config.get("simulation_name", "unknown-simulation")
        self.ip = config.get("ip", "10.0.0.1")
        self.sys_name = config.get("sys_name", f"device-{self.device_id:03d}")
        self.sys_descr = config.get("sys_descr", f"Simulated network device #{self.device_id}")
        self.sys_object_id = config.get("sys_object_id", "1.3.6.1.4.1.8072.3.2.10")
        self.sys_contact = config.get("sys_contact", "admin@localhost")
        self.sys_location = config.get("sys_location", "simulated")
        self.sys_services = config.get("sys_services", 2)
        self.link_speed_mbps = config.get("link_speed_mbps", 1000)
        self.num_interfaces = config.get("num_interfaces", 1)
        self.community = config.get("community", "public")
        self.random_seed = int(config.get("random_seed", self.device_id))

        self.is_online = True
        self.is_powered_on = True
        self._offline_until: float | None = None

        # Use shared uptime tracking
        initial_seconds = config.get("initial_uptime_seconds", 0)
        self.uptime_tracker = UptimeTracker(initial_seconds)

        # Use shared traffic counters
        # Also use TrafficProfile for time-of-day-based rate period switching.
        self.counters: dict[int, TrafficCounter] = {}
        self.traffic_profile: TrafficProfile | None = None
        traffic_profile = config.get("traffic_profile")
        if traffic_profile:
            # Build the full profile dict including periods and timezone
            profile_dict = {
                "interfaces": self.num_interfaces,
                "link_speed_mbps": traffic_profile.get("link_speed_mbps", self.link_speed_mbps),
                "default_rate_mbps": {
                    "min": traffic_profile.get("rate_min", 0),
                    "max": traffic_profile.get("rate_max", 10),
                },
                "variation_percent": traffic_profile.get("variation_percent", 10),
                "periods": traffic_profile.get("periods", []),
                "timezone": traffic_profile.get("timezone", "UTC"),
                "random_seed": self.random_seed,
            }
            self.traffic_profile = TrafficProfile(profile_dict)
            # Initialize counters from the profile
            for i in range(1, self.num_interfaces + 1):
                idx = i - 1
                if idx < len(self.traffic_profile.counters):
                    self.counters[i] = self.traffic_profile.counters[idx]
        else:
            for i in range(1, self.num_interfaces + 1):
                self.counters[i] = TrafficCounter(
                    link_speed_mbps=self.link_speed_mbps,
                    rate_min_mbps=0,
                    rate_max_mbps=10,
                    variation_percent=10,
                    seed=self.random_seed + (i - 1),
                )

        # Use shared outage scheduler
        outage_profile_cfg = config.get("outage_profile")
        self.outage_scheduler: OutageScheduler | None = None
        if outage_profile_cfg:
            profile = OutageProfile(
                enabled=outage_profile_cfg.get("enabled", False),
                expected_events_per_day=outage_profile_cfg.get("expected_events_per_day", 0),
                duration_min_minutes=outage_profile_cfg.get("duration_min_minutes", 0),
                duration_max_minutes=outage_profile_cfg.get("duration_max_minutes", 0),
                minimum_uptime_minutes=outage_profile_cfg.get("minimum_uptime_minutes", 0),
            )
            seed = config.get("random_seed", self.device_id)
            self.outage_scheduler = OutageScheduler(profile, seed)
            # Start scheduler at current time (simulated time = wall clock)
            self.outage_scheduler.set_initial_time(time.time())

    def get_value(self, oid: tuple, index: int | None = None):
        """Get an SNMP value for the given OID. Returns None if offline or noSuchObject."""
        # Check outage scheduler for offline state
        if self.outage_scheduler and not self.outage_scheduler.is_online:
            return None

        if not self.is_online:
            return None

        # System group
        if oid == self.SYS_DESCR_OID:
            return ("OctetString", self.sys_descr)
        if oid == self.SYS_OBJECT_ID_OID:
            return ("ObjectIdentifier", self.sys_object_id)
        if oid == self.SYS_UP_TIME_OID:
            # Use shared uptime tracker
            return ("TimeTicks", self.uptime_tracker.get_uptime_ticks())
        if oid == self.SYS_CONTACT_OID:
            return ("OctetString", self.sys_contact)
        if oid == self.SYS_NAME_OID:
            return ("OctetString", self.sys_name)
        if oid == self.SYS_LOCATION_OID:
            return ("OctetString", self.sys_location)
        if oid == self.SYS_SERVICES_OID:
            return ("Integer", self.sys_services)

        # Interfaces
        if oid == self.IF_NUMBER_OID:
            return ("Integer", self.num_interfaces)

        # Indexed interface objects only exist for configured interface indexes.
        # An out-of-range index is a missing instance, not a synthetic zero-value row.
        if index is not None and not (1 <= index <= self.num_interfaces):
            return None

        if oid == self.IF_INDEX_OID_BASE and index is not None:
            return ("Integer", index)
        if oid == self.IF_DESCR_OID_BASE and index is not None:
            return ("OctetString", f"eth{index - 1}")
        if oid == self.IF_TYPE_OID_BASE and index is not None:
            return ("Integer", 6)  # ethernetCsmacd
        if oid == self.IF_MTU_OID_BASE and index is not None:
            return ("Integer", 1500)
        if oid == self.IF_SPEED_OID_BASE and index is not None:
            # ifSpeed is Gauge32 per RFC 2863
            return ("Gauge32", self.link_speed_mbps * 1_000_000)
        if oid == self.IF_PHYS_ADDRESS_OID_BASE and index is not None:
            return ("OctetString", bytes([
                0x02, 0x00, 0x00, self.device_id & 0xFF,
                (index >> 8) & 0xFF, index & 0xFF,
            ]))
        if oid == self.IF_ADMIN_STATUS_OID_BASE and index is not None:
            return ("Integer", 1)  # up
        if oid == self.IF_OPER_STATUS_OID_BASE and index is not None:
            return ("Integer", 1 if self.is_online else 2)  # down
        if oid == self.IF_IN_OCTETS_OID_BASE and index is not None:
            c = self.counters.get(index)
            if c:
                vals = c.get_values()
                return ("Counter32", vals["ifInOctets_32"])
            return ("Counter32", 0)
        if oid == self.IF_OUT_OCTETS_OID_BASE and index is not None:
            c = self.counters.get(index)
            if c:
                vals = c.get_values()
                return ("Counter32", vals["ifOutOctets_32"])
            return ("Counter32", 0)
        if oid == self.IF_IN_ERRORS_OID_BASE and index is not None:
            return ("Counter32", 0)
        if oid == self.IF_OUT_ERRORS_OID_BASE and index is not None:
            return ("Counter32", 0)
        if oid == self.IF_IN_DISCARDS_OID_BASE and index is not None:
            return ("Counter32", 0)
        if oid == self.IF_OUT_DISCARDS_OID_BASE and index is not None:
            return ("Counter32", 0)
        if oid == self.IF_NAME_OID_BASE and index is not None:
            return ("OctetString", f"eth{index - 1}")
        if oid == self.IF_HIGH_SPEED_OID_BASE and index is not None:
            # ifHighSpeed is Gauge32 per RFC 2863
            return ("Gauge32", self.link_speed_mbps)
        if oid == self.IF_HC_IN_OCTETS_OID_BASE and index is not None:
            c = self.counters.get(index)
            if c:
                vals = c.get_values()
                return ("Counter64", vals["ifHCInOctets_64"])
            return ("Counter64", 0)
        if oid == self.IF_HC_OUT_OCTETS_OID_BASE and index is not None:
            c = self.counters.get(index)
            if c:
                vals = c.get_values()
                return ("Counter64", vals["ifHCOutOctets_64"])
            return ("Counter64", 0)

        return None  # noSuchObject

    def oid_matches(self, oid: tuple, pattern: tuple) -> bool:
        """Check if oid starts with pattern."""
        return len(oid) >= len(pattern) and oid[:len(pattern)] == pattern

    def oid_after(self, oid: tuple, pattern: tuple) -> bool:
        """Check if oid is lexicographically after pattern."""
        prefix_len = len(pattern)
        if len(oid) <= prefix_len:
            return False
        if oid[:prefix_len] == pattern:
            return False
        return oid[:min(prefix_len, len(oid))] >= pattern

    def update_counters(self):
        """Advance traffic counters while the emulated device is powered on."""
        if not self.is_online:
            return
        if self.outage_scheduler and not self.outage_scheduler.is_online:
            return

        now = time.time()
        if self.traffic_profile is not None:
            self.traffic_profile.update_all(now)
        else:
            for counter in self.counters.values():
                counter.update(now)

    def power_off(self) -> bool:
        """Enter offline state without resetting runtime state.

        Reset semantics belong to recovery: uptime and byte counters are reset
        exactly once when the simulated device powers back on.
        """
        if not self.is_powered_on and not self.is_online:
            return False
        self.is_powered_on = False
        self.is_online = False
        return True

    def power_on(self) -> bool:
        """Recover from an outage and reset uptime/counters exactly once."""
        if self.is_powered_on and self.is_online:
            return False

        now = time.time()
        self.uptime_tracker.reset()
        if self.traffic_profile is not None:
            self.traffic_profile.reset_all(now)
        else:
            for counter in self.counters.values():
                counter.reset(now)

        self.is_powered_on = True
        self.is_online = True
        return True


# ---------------------------------------------------------------------------
# SNMPv2c Protocol Encoding / Decoding (RFC 3416)
# ---------------------------------------------------------------------------

def encode_tlv(etype: int, value: bytes) -> bytes:
    """Encode ASN.1 TLV."""
    # Handle long form length
    length = len(value)
    if length < 0x80:
        return bytes([etype, length]) + value
    elif length < 0x100:
        return bytes([etype, 0x81, length]) + value
    else:
        return bytes([etype, 0x82, (length >> 8) & 0xFF, length & 0xFF]) + value


def encode_oid(oid: tuple) -> bytes:
    """Encode OID tuple to raw ASN.1 OID bytes (without TLV wrapper)."""
    if len(oid) < 2:
        return b''
    # First two components: 40*first + second
    result = bytes([oid[0] * 40 + oid[1]])
    # Remaining components use base-128 encoding
    for component in oid[2:]:
        if component == 0:
            result += b'\x00'
        else:
            # Encode in base-128 (high bit set on all but last byte)
            segments = []
            val = component
            segments.append(val & 0x7F)
            val >>= 7
            while val > 0:
                segments.append((val & 0x7F) | 0x80)
                val >>= 7
            result += bytes(reversed(segments))
    return result


def encode_integer(value: int) -> bytes:
    if value >= 0:
        if value < 128:
            return encode_tlv(0x02, bytes([value]))
        elif value < 256:
            return encode_tlv(0x02, bytes([0, value & 0xFF]))
        elif value < 65536:
            return encode_tlv(0x02, bytes([(value >> 8) & 0xFF, value & 0xFF]))
        else:
            h = hex(value)[2:].rjust(8, '0')
            return encode_tlv(0x02, bytes.fromhex(h[:4]) + bytes.fromhex(h[4:]))
    else:
        if value > -128:
            return encode_tlv(0x02, bytes([value & 0xFF]))
        else:
            val = value & 0xFFFFFFFF
            h = hex(val)[2:].rjust(8, '0')
            return encode_tlv(0x02, bytes.fromhex(h[:4]) + bytes.fromhex(h[4:]))


# SNMP Application data type BER tags (context-specific class, per RFC 2578)
# Counter32 ::= [APPLICATION 1] INTEGER  -> 0x81 (high bit set: class=application (0x40) | tag=1 = 0x81)
# Gauge32   ::= [APPLICATION 2] INTEGER  -> 0x82
# TimeTicks ::= [APPLICATION 3] INTEGER  -> 0x83
# Counter64 ::= [APPLICATION 6] INTEGER  -> 0x86
#
# Note: [APPLICATION n] has high bit (0x40) set. So:
#   Counter32 [APPLICATION 1] = 0x40 | 1 = 0x41
#   Gauge32   [APPLICATION 2] = 0x40 | 2 = 0x42
#   TimeTicks [APPLICATION 3] = 0x40 | 3 = 0x43
#   Counter64 [APPLICATION 6] = 0x40 | 6 = 0x46
TAG_COUNTER32 = 0x41  # [APPLICATION 1] per RFC 2578
TAG_GAUGE32 = 0x42  # [APPLICATION 2] per RFC 2578
TAG_TIMETICKS = 0x43  # [APPLICATION 3] per RFC 2578
TAG_COUNTER64 = 0x46  # [APPLICATION 6] per RFC 2578


def encode_counter32(value: int) -> bytes:
    val = value & 0xFFFFFFFF
    h = hex(val)[2:].rjust(8, '0')
    return encode_tlv(TAG_COUNTER32, bytes.fromhex(h[:4]) + bytes.fromhex(h[4:]))


def encode_counter64(value: int) -> bytes:
    val = value & 0xFFFFFFFFFFFFFFFF
    h = hex(val)[2:].rjust(16, '0')
    return encode_tlv(TAG_COUNTER64, bytes.fromhex(h[:4]) + bytes.fromhex(h[4:8]) + bytes.fromhex(h[8:12]) + bytes.fromhex(h[12:]))


def encode_timeticks(value: int) -> bytes:
    """Encode TimeTicks as [APPLICATION 3] INTEGER (not Counter32)."""
    val = value & 0xFFFFFFFF
    h = hex(val)[2:].rjust(8, '0')
    return encode_tlv(TAG_TIMETICKS, bytes.fromhex(h[:4]) + bytes.fromhex(h[4:]))


def encode_string(s: str) -> bytes:
    if isinstance(s, str):
        return encode_tlv(0x04, s.encode('utf-8', errors='replace'))
    return encode_tlv(0x04, s)


def encode_varbind(oid: tuple, value_type: str, value) -> bytes:
    """Encode a single variable binding."""
    oid_bytes = encode_oid(oid)
    # Wrap OID in its own TLV (OBJECT IDENTIFIER type = 0x06)
    oid_tlv = encode_tlv(0x06, oid_bytes)
    if value_type == "Integer":
        val_bytes = encode_integer(value)
    elif value_type == "Counter32":
        val_bytes = encode_counter32(value)
    elif value_type == "Counter64":
        val_bytes = encode_counter64(value)
    elif value_type == "Gauge32":
        val_bytes = encode_tlv(TAG_GAUGE32, (value & 0xFFFFFFFF).to_bytes(4, 'big'))
    elif value_type == "TimeTicks":
        val_bytes = encode_timeticks(value)
    elif value_type == "OctetString":
        val_bytes = encode_string(value)
    elif value_type == "ObjectIdentifier":
        # Encode as ObjectIdentifier [APPLICATION 6] -> BER tag 0x06
        oid_bytes = encode_oid(tuple(int(x) for x in str(value).split('.')))
        val_bytes = encode_tlv(0x06, oid_bytes)
    else:
        val_bytes = encode_integer(0)

    return encode_tlv(0x30, oid_tlv + val_bytes)  # SEQUENCE { OID, Value }


def encode_varbind_list(varbinds: list) -> bytes:
    """Encode a list of (oid_tuple, value_type, value) tuples."""
    parts = b""
    for vb in varbinds:
        parts += encode_varbind(vb[0], vb[1], vb[2])
    return encode_tlv(0x30, parts)  # SEQUENCE


def parse_tl(data: bytes, offset: int):
    """Read a single TLV. Returns (type, value_bytes, new_offset)."""
    if offset >= len(data):
        return None, None, offset
    etype = data[offset]
    offset += 1
    if offset >= len(data):
        return etype, b'', offset
    length = data[offset]
    offset += 1
    if length & 0x80:
        num_bytes = length & 0x7F
        if offset + num_bytes > len(data):
            return etype, b'', offset
        length = 0
        for _ in range(num_bytes):
            length = (length << 8) | data[offset]
            offset += 1
    if offset + length > len(data):
        return etype, data[offset:], offset
    value = data[offset:offset + length]
    return etype, value, offset + length


def decode_oid(data: bytes) -> tuple:
    """Decode an OID from bytes."""
    if len(data) < 1:
        return ()
    result = [data[0] // 40, data[0] % 40]
    i = 1
    while i < len(data):
        val = 0
        while True:
            val = (val << 7) | (data[i] & 0x7F)
            i += 1
            if not (data[i - 1] & 0x80):
                break
        result.append(val)
    return tuple(result)


def parse_snmp_packet(data: bytes):
    """Parse an SNMP packet. Returns (version, community, pdu_type, varbinds, request_id).
    varbinds is a list of (oid_tuple,) for requests, or list of (oid_tuple, value_type, value) for responses."""
    offset = 0
    # SEQUENCE
    et, val, offset = parse_tl(data, offset)
    inner_offset = 0
    # version
    et, ver_bytes, inner_offset = parse_tl(val, inner_offset)
    version = int(ver_bytes[0]) if ver_bytes else 2
    # community
    et, comm_bytes, inner_offset = parse_tl(val, inner_offset)
    community = comm_bytes.decode('utf-8', errors='replace') if comm_bytes else ''
    # PDU
    pdu_type, pdu_bytes, inner_offset = parse_tl(val, inner_offset)
    pdu_offset = 0
    et, request_id_bytes, pdu_offset = parse_tl(pdu_bytes, pdu_offset)
    request_id = int.from_bytes(request_id_bytes, 'big') if request_id_bytes else 0

    # GetBulk (0xA5) has different format: request_id, nonRepeaters,
    # maxRepetitions, varbinds.
    # Get/GetNext/Response (0xA0/0xA1/0xA2) have: request_id, error_status, error_index, varbinds
    if pdu_type == 0xA5:  # GetBulkRequest ::= [5] IMPLICIT BulkPDU
        et, non_rep_bytes, pdu_offset = parse_tl(pdu_bytes, pdu_offset)
        non_rep = int.from_bytes(non_rep_bytes, 'big') if non_rep_bytes else 0
        et, max_rep_bytes, pdu_offset = parse_tl(pdu_bytes, pdu_offset)
        max_rep = int.from_bytes(max_rep_bytes, 'big') if max_rep_bytes else 0
    else:
        et, error_status_bytes, pdu_offset = parse_tl(pdu_bytes, pdu_offset)
        error_status = int(error_status_bytes[0]) if error_status_bytes else 0
        et, error_index_bytes, pdu_offset = parse_tl(pdu_bytes, pdu_offset)
        error_index = int(error_index_bytes[0]) if error_index_bytes else 0

    # varbind list
    et, vb_bytes, pdu_offset = parse_tl(pdu_bytes, pdu_offset)
    varbinds = []
    vb_off = 0
    if vb_bytes is None:
        vb_bytes = b''
    while vb_off < len(vb_bytes):
        vb_type, vb_val, vb_off = parse_tl(vb_bytes, vb_off)
        # varbind is a SEQUENCE
        vbo = 0
        oid_et, oid_bytes, vbo = parse_tl(vb_val, vbo)
        oid = decode_oid(oid_bytes) if oid_bytes else ()
        val_et, val_bytes, vbo = parse_tl(vb_val, vbo)
        varbinds.append((oid, val_et, val_bytes))
    return version, community, pdu_type, varbinds, request_id, (non_rep if pdu_type == 0xA5 else 0), (max_rep if pdu_type == 0xA5 else 0)


def build_snmp_response(version: int, community: str, request_id: int, error_status: int,
                        error_index: int, varbind_list: bytes, pdu_type: int = 0x06) -> bytes:
    """Build SNMP response packet.

    Per RFC 3416 Section 2.2, Response-PDU (0xA2) content is:
      request-id (INTEGER) + error-status (INTEGER) + error-index (INTEGER) + varbinds
    """
    request_id_bytes = request_id.to_bytes(4, 'big')
    error_status_bytes = error_status.to_bytes(4, 'big')
    error_index_bytes = error_index.to_bytes(4, 'big')
    pdu_content = (
        b'\x02\x04' + request_id_bytes  # request_id INTEGER
        + b'\x02\x04' + error_status_bytes  # error_status INTEGER
        + b'\x02\x04' + error_index_bytes  # error_index INTEGER
        + varbind_list  # varbind list (SEQUENCE)
    )
    # PDU is a TLV: type + encoded content
    pdu = encode_tlv(pdu_type, pdu_content)
    # SEQUENCE
    community_bytes = community.encode('utf-8')
    # SNMPv2c uses version 1 (SNMPv2) in the version field: 02 01 01
    # Per RFC 3416, SNMPv2c reuses the SNMPv2 version number (1) but uses community string authentication
    ver_bytes = b'\x02\x01\x01'  # version = 1 (SNMPv2c)
    msg = ver_bytes + bytes([0x04, len(community_bytes)]) + community_bytes + pdu
    return encode_tlv(0x30, msg)


# ---------------------------------------------------------------------------
# SNMP UDP Handler (pure Python, no external library)
# ---------------------------------------------------------------------------

class SNMPUDPHandler:
    """Handles SNMPv2c requests on UDP port 161."""

    # Context-specific constructed PDU tags per RFC 3416.
    # GetRequest-PDU      ::= [0] IMPLICIT PDU
    # GetNextRequest-PDU  ::= [1] IMPLICIT PDU
    # Response-PDU        ::= [2] IMPLICIT PDU
    # GetBulkRequest-PDU  ::= [5] IMPLICIT BulkPDU
    SNMP_GET = 0xA0
    SNMP_GETNEXT = 0xA1
    SNMP_GETBULK = 0xA5
    SNMP_RESPONSE = 0xA2

    def __init__(self, data_store: SNMPDataStore, port: int = 161):
        self.data = data_store
        self.port = port
        self.sock = None
        self._running = False
        self._thread = None
        self._heartbeat = WorkerHeartbeat()

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('', self.port))
        self.sock.settimeout(0.5)
        self._running = True
        self._heartbeat.beat()
        self._thread = threading.Thread(
            target=self._snmp_loop,
            name="snmp-udp",
            daemon=True,
        )
        self._thread.start()
        print(f"SNMP agent listening on 0.0.0.0:{self.port}", flush=True)

    def _snmp_loop(self):
        _set_kernel_thread_name("snmp-udp")
        while self._running:
            self._heartbeat.beat()
            try:
                data, addr = self.sock.recvfrom(65535)
                if not self.data.is_online:
                    continue
                self._handle_request(data, addr)
            except socket.timeout:
                continue
            except Exception:
                logger = __import__('logging').getLogger('snmp_handler')
                logger.error("SNMP handler error: %s", exc_info=True)
                continue

    def _handle_request(self, data: bytes, addr: tuple):
        community = ""
        pdu_type = 0
        varbinds = []
        request_id = 0
        non_rep = 0
        max_rep = 0
        try:
            result = parse_snmp_packet(data)
            if len(result) == 7:
                version, community, pdu_type, varbinds, request_id, non_rep, max_rep = result
            else:
                version, community, pdu_type, varbinds, request_id = result
                non_rep = 0
                max_rep = 0
            if community != self.data.community:
                return  # Invalid community - don't respond

            # Update counters before responding
            self.data.update_counters()

            if pdu_type == self.SNMP_GET:
                response_vb = self._handle_get(varbinds)
            elif pdu_type == self.SNMP_GETNEXT:
                response_vb = self._handle_getnext(varbinds)
            elif pdu_type == self.SNMP_GETBULK:
                response_vb = self._handle_getbulk(varbinds, non_rep, max_rep)
            else:
                return

            self.sock.sendto(
                build_snmp_response(version, community, request_id, 0, 0, response_vb, self.SNMP_RESPONSE),
                addr
            )
            print(f"Sent SNMP response to {addr}, varbinds: {len(varbinds)}", flush=True)
        except Exception as e:
            import traceback
            print(f"SNMP handler error: {e}", flush=True)
            print(f"Traceback: {traceback.format_exc()}", flush=True)

    def _handle_get(self, varbinds: list) -> bytes:
        """Handle SNMP GET per RFC 3416 section 4.2.1.

        A name that identifies a known object but not an existing instance
        yields noSuchInstance.  A name outside every accessible object prefix
        yields noSuchObject.
        """
        result = b""
        for oid, val_et, val_bytes in varbinds:
            idx = None
            matched_base = None
            for base in self._IF_BASES:
                if len(oid) >= len(base) and oid[:len(base)] == base:
                    # A valid tabular instance has exactly one index component.
                    if len(oid) == len(base) + 1:
                        idx = oid[len(base)]
                    matched_base = base
                    break

            if matched_base is not None and idx is not None:
                val = self.data.get_value(matched_base, index=idx)
            else:
                val = self.data.get_value(oid)

            if val:
                val_type, val_val = val
                result += encode_varbind(oid, val_type, val_val)
            else:
                if self._has_accessible_object_prefix(oid):
                    result += self._encode_no_such_instance(oid)
                else:
                    result += self._encode_no_such_object(oid)
        return encode_tlv(0x30, result)

    def _handle_getnext(self, varbinds: list) -> bytes:
        """Handle GetNext by finding the next OID in lexicographic order.

        Per SNMPv2c (RFC 3416):
        - If a next OID exists, return it.
        - If the OID is past all known OIDs, return endOfMibView (0x82)
          to signal the end of a walk.
        """
        result = b""
        all_known_oid_keys = self._get_all_oid_keys()

        for oid, val_et, val_bytes in varbinds:
            matched = self._find_next(oid, all_known_oid_keys)
            if matched:
                matched_oid, val_type, val_val = matched
                result += encode_varbind(matched_oid, val_type, val_val)
            else:
                # endOfMibView per RFC 3416: signals end of MIB view during walk
                result += self._encode_end_of_mib_view(oid)
        return encode_tlv(0x30, result)

    def _handle_getbulk(self, varbinds: list, non_rep: int, max_rep: int) -> bytes:
        """Handle GetBulk request per RFC 3416.

        N = min(non-repeaters, number of request varbinds)
        M = max-repetitions
        R = number of request varbinds - N

        The N non-repeaters each advance once.  The R repeater columns then
        advance for up to M rows, in row-major order as required by RFC 3416.
        """
        all_known_oid_keys = self._get_all_oid_keys()
        result = b""
        num_varbinds = len(varbinds)

        # RFC fields are constrained to non-negative values.  Be defensive
        # about malformed input rather than letting Python ranges behave oddly.
        non_rep = max(0, non_rep)
        max_rep = max(0, max_rep)
        n = min(non_rep, num_varbinds)

        # Process non-repeaters: single GetNext step each
        for i in range(n):
            oid, val_et, val_bytes = varbinds[i]
            matched = self._find_next(oid, all_known_oid_keys)
            if matched:
                matched_oid, val_type, val_val = matched
                result += encode_varbind(matched_oid, val_type, val_val)
            else:
                result += self._encode_end_of_mib_view(oid)

        # All remaining request varbinds are repeater columns.  Each row is
        # emitted left-to-right before advancing to the next repetition.
        repeaters = [vb[0] for vb in varbinds[n:]]
        if max_rep > 0 and repeaters:
            current_oids = list(repeaters)
            exhausted = [False] * len(current_oids)

            for _ in range(max_rep):
                row_all_end = True
                for column in range(len(current_oids)):
                    current_oid = current_oids[column]

                    if exhausted[column]:
                        result += self._encode_end_of_mib_view(current_oid)
                        continue

                    matched = self._find_next(current_oid, all_known_oid_keys)
                    if matched:
                        matched_oid, val_type, val_val = matched
                        result += encode_varbind(matched_oid, val_type, val_val)
                        current_oids[column] = matched_oid
                        row_all_end = False
                    else:
                        result += self._encode_end_of_mib_view(current_oid)
                        exhausted[column] = True

                # RFC 3416 permits truncating the response once an entire
                # repetition consists of endOfMibView values.
                if row_all_end:
                    break

        return encode_tlv(0x30, result)

    def _get_all_oid_keys(self) -> list:
        """Get all known OID keys in sorted order for GetNext resolution.

        Includes system OIDs and every configured interface instance for each
        supported column.  This is the authoritative lexicographic view used
        by GetNext and GetBulk.
        """
        keys = set()
        # System OIDs (no index, leaf OIDs ending in 0)
        system_oids = [
            self.data.SYS_DESCR_OID, self.data.SYS_OBJECT_ID_OID, self.data.SYS_UP_TIME_OID,
            self.data.SYS_CONTACT_OID, self.data.SYS_NAME_OID, self.data.SYS_LOCATION_OID,
            self.data.SYS_SERVICES_OID, self.data.IF_NUMBER_OID,
        ]
        keys.update(system_oids)
        # Interface OIDs — add every configured instance for every column.
        iface_bases = [
            self.data.IF_INDEX_OID_BASE, self.data.IF_DESCR_OID_BASE, self.data.IF_TYPE_OID_BASE,
            self.data.IF_MTU_OID_BASE, self.data.IF_SPEED_OID_BASE, self.data.IF_PHYS_ADDRESS_OID_BASE,
            self.data.IF_ADMIN_STATUS_OID_BASE, self.data.IF_OPER_STATUS_OID_BASE,
            self.data.IF_IN_OCTETS_OID_BASE, self.data.IF_OUT_OCTETS_OID_BASE,
            self.data.IF_IN_ERRORS_OID_BASE, self.data.IF_OUT_ERRORS_OID_BASE,
            self.data.IF_IN_DISCARDS_OID_BASE, self.data.IF_OUT_DISCARDS_OID_BASE,
            self.data.IF_NAME_OID_BASE, self.data.IF_HIGH_SPEED_OID_BASE,
            self.data.IF_HC_IN_OCTETS_OID_BASE, self.data.IF_HC_OUT_OCTETS_OID_BASE,
        ]
        for base in iface_bases:
            for index in range(1, self.data.num_interfaces + 1):
                keys.add(base + (index,))
        return sorted(keys)

    # Set of interface-base OID tuples for fast lookup in _find_next
    _IF_BASES = frozenset([
        SNMPDataStore.IF_INDEX_OID_BASE, SNMPDataStore.IF_DESCR_OID_BASE, SNMPDataStore.IF_TYPE_OID_BASE,
        SNMPDataStore.IF_MTU_OID_BASE, SNMPDataStore.IF_SPEED_OID_BASE, SNMPDataStore.IF_PHYS_ADDRESS_OID_BASE,
        SNMPDataStore.IF_ADMIN_STATUS_OID_BASE, SNMPDataStore.IF_OPER_STATUS_OID_BASE,
        SNMPDataStore.IF_IN_OCTETS_OID_BASE, SNMPDataStore.IF_OUT_OCTETS_OID_BASE,
        SNMPDataStore.IF_IN_ERRORS_OID_BASE, SNMPDataStore.IF_OUT_ERRORS_OID_BASE,
        SNMPDataStore.IF_IN_DISCARDS_OID_BASE, SNMPDataStore.IF_OUT_DISCARDS_OID_BASE,
        SNMPDataStore.IF_NAME_OID_BASE, SNMPDataStore.IF_HIGH_SPEED_OID_BASE,
        SNMPDataStore.IF_HC_IN_OCTETS_OID_BASE, SNMPDataStore.IF_HC_OUT_OCTETS_OID_BASE,
    ])

    # Scalar object identifiers do not include the instance sub-identifier
    # (.0).  These prefixes, together with the table-column OIDs above, let
    # GET distinguish noSuchObject from noSuchInstance exactly as RFC 3416
    # section 4.2.1 requires.
    _SCALAR_OBJECT_BASES = frozenset([
        SNMPDataStore.SYS_DESCR_OID[:-1],
        SNMPDataStore.SYS_OBJECT_ID_OID[:-1],
        SNMPDataStore.SYS_UP_TIME_OID[:-1],
        SNMPDataStore.SYS_CONTACT_OID[:-1],
        SNMPDataStore.SYS_NAME_OID[:-1],
        SNMPDataStore.SYS_LOCATION_OID[:-1],
        SNMPDataStore.SYS_SERVICES_OID[:-1],
        SNMPDataStore.IF_NUMBER_OID[:-1],
    ])

    def _has_accessible_object_prefix(self, oid: tuple) -> bool:
        """Return True when *oid* lies under a supported MIB object."""
        for base in self._SCALAR_OBJECT_BASES:
            if len(oid) >= len(base) and oid[:len(base)] == base:
                return True
        for base in self._IF_BASES:
            if len(oid) >= len(base) and oid[:len(base)] == base:
                return True
        return False

    def _find_next(self, target_oid: tuple, known_keys: list) -> tuple:
        """Find the next OID after *target_oid* that we can serve.

        Returns ``(oid_tuple, value_type_str, value)`` or ``None`` when
        there is no following instance (caller emits noSuchInstance).
        """
        logger = __import__('logging').getLogger('snmp_findnext')
        logger.debug("Looking for next OID after %s in %d keys", target_oid, len(known_keys))
        for key in known_keys:
            if key > target_oid:
                # Determine whether *key* is a system-leaf (ends in 0, length
                # matches a known system base) or an interface leaf.
                val = None
                # Try with index first (interface OIDs store with index)
                if len(key) > 1:
                    idx = key[-1]
                    # Check if key matches any interface base pattern
                    for ib in self._IF_BASES:
                        if key[:len(ib)] == ib:
                            val = self.data.get_value(ib, idx)
                            logger.debug("Matched IF base %s with idx=%s, val=%s", ib, idx, val)
                            break
                if val is None:
                    val = self.data.get_value(key)
                    logger.debug("get_value(%s) returned %s", key, val)
                if val is not None:
                    logger.debug("Returning match: %s -> %s", key, val)
                    return (key, val[0], val[1])
        logger.debug("No match found for %s", target_oid)
        return None

    @staticmethod
    def _encode_no_such_object(oid: tuple) -> bytes:
        """Encode a noSuchObject variable-binding per RFC 3416 Section 4.2.

        Returns a varbind where the value is context-specific tag 0x80
        with zero-length content, indicating the requested object does not
        exist at all (not just the instance). The OID in the response matches
        the requested OID.

        Per RFC 3416 Section 4.2:
        "the varBindValue field contains a reference to the object that
        could not be found (noSuchObject)"

        The exception syntax uses context-specific primitive tag [0] IMPLICIT
        which is BER tag 0x80 with zero-length content: b'\\x80\\x00'
        """
        oid_bytes = encode_oid(oid)
        oid_tlv = encode_tlv(0x06, oid_bytes)
        return encode_tlv(0x30, oid_tlv + b'\x80\x00')

    @staticmethod
    def _encode_no_such_instance(oid: tuple) -> bytes:
        """Encode a noSuchInstance variable-binding per RFC 3416 Section 4.2.1.

        Returns a varbind where the value is context-specific tag 0x81
        with zero-length content, indicating the requested OID does not
        exist on this agent. The OID in the response matches the requested OID.

        Per RFC 3416 Section 4.2.1:
        "the varBindValue field contains a reference to the object that
        could not be found (noSuchInstance)"

        The exception syntax uses context-specific primitive tag [1] IMPLICIT
        which is BER tag 0x81 with zero-length content: b'\\x81\\x00'
        """
        oid_bytes = encode_oid(oid)
        oid_tlv = encode_tlv(0x06, oid_bytes)
        return encode_tlv(0x30, oid_tlv + b'\x81\x00')

    @staticmethod
    def _encode_end_of_mib_view(oid: tuple) -> bytes:
        """Encode an endOfMibView variable-binding per RFC 3416 Section 4.2.2.

        Used to signal the end of an SNMP walk. Per RFC 3416 Section 4.2.2:
        "the varBindValue field contains an endOfMibView exception"

        The varBindName is the requested OID, and the varBindValue is
        context-specific tag 0x82 with zero-length content.

        Per RFC 3416 Section 4.2.2:
        "the varBindValue field contains a reference to the last
        successfully retrieved OID (endOfMibView)"

        The exception syntax uses context-specific primitive tag [2] IMPLICIT
        which is BER tag 0x82 with zero-length content: b'\\x82\\x00'
        """
        oid_bytes = encode_oid(oid)
        oid_tlv = encode_tlv(0x06, oid_bytes)
        return encode_tlv(0x30, oid_tlv + b'\x82\x00')

    def stop(self):
        self._running = False
        if self.sock:
            self.sock.close()

    def is_alive(self) -> bool:
        """True while the receive loop thread is still running."""
        return self._thread is not None and self._thread.is_alive()

    def heartbeat_stale(self) -> bool:
        """True if the receive loop stopped beating (killed or wedged)."""
        return self._heartbeat.stale()


# ---------------------------------------------------------------------------
# Power Control (HTTP control API + outage scheduler, single state owner)
# ---------------------------------------------------------------------------

# Control API port: keep in sync with CONTROL_PORT in host/swarm.py.
CONTROL_PORT = 8080


def _is_authorized(peer: str, management_subnet: str | None) -> bool:
    """Authorize a control-API peer.

    Loopback is always allowed (host-side management). Otherwise the peer must
    be inside the management subnet; with no configured subnet, only loopback
    is allowed.
    """
    import ipaddress

    if peer in ("127.0.0.1", "::1"):
        return True
    if not management_subnet:
        return False
    try:
        net = ipaddress.ip_network(management_subnet, strict=False)
        return ipaddress.ip_address(peer) in net
    except ValueError:
        return False


class PowerController:
    """Single owner of the emulated device's power state.

    The device is online only when the manual power switch is ON and the
    outage scheduler (if any) reports online. Every transition goes through
    this class under one lock, so the HTTP control thread and the scheduler
    loop in the main thread can never race.

    Manual commands:
      - OFF persists until a manual ON, even across scheduled outages.
      - ON during a scheduled outage keeps the device offline until the
        outage ends; the scheduler's power-on event then brings it up.
        (A manual ON does not cancel future scheduled outages.)

    Uptime/counter reset semantics are unchanged: they reset exactly once,
    on the transition into the online state (data.power_on()).
    """

    def __init__(self, data: SNMPDataStore, device_id: int, device_name: str,
                 device_ip: str, simulation_name: str,
                 control_port: int = CONTROL_PORT):
        self.data = data
        self.device_id = device_id
        self.device_name = device_name
        self.device_ip = device_ip
        self.simulation_name = simulation_name
        self._lock = threading.Lock()
        self._manual_off = False
        self._control_port = control_port

    # -- state helpers (call with self._lock held) ---------------------------

    def _scheduler_online(self) -> bool:
        sched = self.data.outage_scheduler
        return sched is None or sched.is_online

    def _effective_online(self) -> bool:
        return (not self._manual_off) and self._scheduler_online()

    def _power_state(self) -> str:
        return "offline" if not self._effective_online() else "online"

    # -- manual commands (HTTP control API) -----------------------------------

    def manual_power_off(self) -> dict:
        with self._lock:
            if not self._manual_off:
                if self._block_network():
                    self._manual_off = True
                    self.data.power_off()
                    log_event(
                        self.simulation_name, self.device_id,
                        "Powering OFF (manual control); emulated device powered off",
                    )
                else:
                    # Atomic: the firewall could not be applied, so the
                    # power state was not changed and the failure is
                    # reported instead of a successful transition.
                    status = self._status_locked()
                    status["error"] = ("firewall block failed; power off "
                                       "aborted, device remains online")
                    return status
            return self._status_locked()

    def manual_power_on(self) -> dict:
        with self._lock:
            if not self._manual_off:
                return self._status_locked()
            if not self._scheduler_online():
                # A scheduled outage is in progress: the device stays
                # offline; the scheduler's power-on event will bring it up
                # when the outage ends.
                self._manual_off = False
                return self._status_locked()
            if self._unblock_network():
                self._manual_off = False
                self.data.power_on()
                log_event(
                    self.simulation_name, self.device_id,
                    "Powering ON (manual control); emulated device powered on; "
                    "uptime and counters reset after power cycle",
                )
                return self._status_locked()
            # Atomic: the firewall could not be restored, so the device
            # stays offline (and blocked) and the failure is reported.
            status = self._status_locked()
            status["error"] = ("firewall unblock failed; power on aborted, "
                               "device remains offline")
            return status

    # -- scheduler events (main loop) ------------------------------------------

    def scheduler_power_off(self) -> None:
        with self._lock:
            if self._manual_off:
                return  # already offline via manual control
            if not self.data.is_online:
                return  # no state change possible
            if self._block_network():
                self.data.power_off()
                log_event(
                    self.simulation_name, self.device_id,
                    "Scheduler: Powering OFF; scheduled outage began",
                )
            else:
                log_event(
                    self.simulation_name, self.device_id,
                    "Scheduler: power-off aborted, firewall block failed; "
                    "device remains online",
                )

    def scheduler_power_on(self) -> None:
        with self._lock:
            if self._manual_off:
                return  # manual OFF persists until a manual ON
            if self.data.is_online:
                return  # no state change possible
            if self._unblock_network():
                self.data.power_on()
                log_event(
                    self.simulation_name, self.device_id,
                    "Scheduler: Powering ON; scheduled outage ended; "
                    "uptime and counters reset after power cycle",
                )
            else:
                log_event(
                    self.simulation_name, self.device_id,
                    "Scheduler: power-on aborted, firewall unblock failed; "
                    "device remains offline",
                )

    # -- status ------------------------------------------------------------------

    def _status_locked(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.device_name,
            "ip": self.device_ip,
            "power": self._power_state(),
        }

    def status(self) -> dict:
        with self._lock:
            return self._status_locked()

    # -- network blocking (scoped iptables chains; control port stays reachable)
    #
    # The outage firewall lives only in dedicated chains (SNMPSWARM_IN /
    # SNMPSWARM_OUT) plus one jump rule in each of INPUT and OUTPUT. No rule
    # or chain that SNMPSwarm does not own is ever flushed or modified.
    #
    # Every step is idempotent, so a partially applied block/unblock can be
    # re-run safely. Verified iptables(8) return codes (nf_tables backend,
    # v1.8.11 inside the image):
    #   -N existing chain       -> 1 ("Chain already exists")
    #   -F/-X missing chain     -> 1 ("No chain/target/match by that name.")
    #   -C/-D missing rule      -> 1 ("Bad rule (does a matching rule exist?)")
    #   -C/-D target chain gone -> 2 (invalid rule spec; check chain first)
    #   -C invalid rule spec    -> 2 (hard failure)
    #   -X non-empty chain      -> 4 (EBUSY)
    #   -S <chain>              -> 0 (chain exists) / 1 (chain absent)
    #
    # Concurrency: power transitions hold self._lock, so at most one thread
    # in this process manipulates the firewall; `iptables -w` additionally
    # serializes on the xtables lock against any other process.

    IN_CHAIN = "SNMPSWARM_IN"
    OUT_CHAIN = "SNMPSWARM_OUT"
    IPTABLES_TIMEOUT = 10

    def _run_iptables(self, args: list[str], allowed: tuple[int, ...]) -> int | None:
        """Run `iptables -w <args>`; return the rc if it is in `allowed`.

        Returns None (after logging) on an unexpected rc, timeout, or a
        missing iptables binary. Callers treat None as a hard failure.
        """
        import subprocess
        cmd = ["iptables", "-w", *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.IPTABLES_TIMEOUT)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            log_event(self.simulation_name, self.device_id,
                      f"Firewall error: {' '.join(cmd)} failed: {exc!r}")
            return None
        if proc.returncode not in allowed:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            log_event(self.simulation_name, self.device_id,
                      f"Firewall error: {' '.join(cmd)} rc={proc.returncode} "
                      f"{' '.join(detail)[:200]}")
            return None
        return proc.returncode

    def _ensure_chains(self) -> bool:
        """Create the SNMPSwarm-owned chains if missing and prove write access.

        `-N` is idempotent (rc 1 = already exists); a successful `-F` on the
        resulting chain then proves we can actually write to the tables.
        """
        for chain in (self.IN_CHAIN, self.OUT_CHAIN):
            if self._run_iptables(["-N", chain], (0, 1)) is None:
                return False
            if self._run_iptables(["-F", chain], (0,)) is None:
                return False
        return True

    def _ensure_jump_rule(self, chain: str, builtin: str) -> bool:
        """Install the single `-j <chain>` jump rule in a built-in chain."""
        rc = self._run_iptables(["-C", builtin, "-j", chain], (0, 1))
        if rc is None:
            return False
        if rc == 0:
            return True  # already installed
        return self._run_iptables(["-I", builtin, "1", "-j", chain], (0,)) is not None

    def _chain_exists(self, chain: str) -> bool:
        """True if the chain exists (`iptables -S CHAIN` exits 0 iff it does)."""
        return self._run_iptables(["-S", chain], (0, 1)) == 0

    def _unblock_network(self) -> bool:
        """Remove every SNMPSwarm-owned rule and chain (idempotent).

        Safe to call when nothing is blocked. A chain that no longer exists
        has nothing to remove: `-D INPUT -j CHAIN` with a missing target
        chain is an invalid rule spec (rc=2), so the jump rule is only
        removed while its chain is still present.
        """
        for chain, builtin in ((self.IN_CHAIN, "INPUT"), (self.OUT_CHAIN, "OUTPUT")):
            if not self._chain_exists(chain):
                continue  # already fully removed
            for args, allowed in (
                (["-D", builtin, "-j", chain], (0, 1)),
                (["-F", chain], (0,)),
                (["-X", chain], (0,)),
            ):
                if self._run_iptables(args, allowed) is None:
                    return False
        return True

    def _block_network(self) -> bool:
        """Block device traffic while keeping the control API reachable.

        Inbound control-port traffic is accepted before the blanket DROP so
        GET /status and POST /power/on keep working while OFF; outbound
        RELATED,ESTABLISHED traffic lets in-flight control connections
        complete.

        The firewall is applied before any power-state commit: if it cannot
        be applied, the transition is aborted and the caller must NOT
        report a successful power change.
        """
        if not self._ensure_chains():
            return False
        if not self._ensure_jump_rule(self.IN_CHAIN, "INPUT"):
            return self._abort_block("could not install INPUT jump rule")
        if not self._ensure_jump_rule(self.OUT_CHAIN, "OUTPUT"):
            return self._abort_block("could not install OUTPUT jump rule")
        rules = [
            # Control API first, so a later failure can never cut it off.
            ["-A", self.IN_CHAIN, "-p", "tcp", "--dport",
             str(self._control_port), "-j", "ACCEPT"],
            ["-A", self.IN_CHAIN, "-j", "DROP"],
            ["-A", self.OUT_CHAIN, "-m", "conntrack",
             "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
            ["-A", self.OUT_CHAIN, "-j", "DROP"],
        ]
        for args in rules:
            if self._run_iptables(args, (0,)) is None:
                return self._abort_block(f"rule {' '.join(args)} failed")
        return True

    def _abort_block(self, reason: str) -> bool:
        """Best-effort restore of pass-through after a partial block."""
        log_event(self.simulation_name, self.device_id,
                  f"Firewall block incomplete ({reason}); restoring pass-through")
        self._unblock_network()
        return False

    def firewall_preflight(self) -> bool:
        """Verify at startup that we can manage the SNMPSwarm-owned chains."""
        if self._ensure_chains():
            log_event(self.simulation_name, self.device_id,
                      "Firewall preflight OK: SNMPSwarm-owned chains ready")
            return True
        log_event(self.simulation_name, self.device_id,
                  "Firewall preflight FAILED: cannot manage SNMPSwarm-owned "
                  "iptables chains (missing iptables or CAP_NET_ADMIN?)")
        return False

    def cleanup_firewall(self) -> None:
        """Remove SNMPSwarm-owned rules/chains on termination (best effort)."""
        if not self._unblock_network():
            log_event(self.simulation_name, self.device_id,
                      "Firewall cleanup on shutdown did not complete; rules "
                      "are container-local and die with the container netns")


class ControlHTTPServer:
    """Lightweight per-device control API (stdlib http.server, one daemon thread).

    Endpoints (restricted to loopback and the management subnet):
      GET  /status     -> {"device_id", "name", "ip", "power"}
      POST /power/on   -> power response
      POST /power/off  -> power response

    Errors: 404 unknown path, 405 wrong method on a known path,
    403 peer outside the management network.
    """

    def __init__(self, controller: PowerController, management_subnet: str | None):
        self.controller = controller
        self.management_subnet = management_subnet
        self._server = None
        self._thread: threading.Thread | None = None
        self._heartbeat = WorkerHeartbeat()

    def start(self, port: int = CONTROL_PORT):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        controller = self.controller
        management_subnet = self.management_subnet

        class Handler(BaseHTTPRequestHandler):
            def _authorized(self) -> bool:
                return _is_authorized(self.client_address[0], management_subnet)

            def _send_json(self, code: int, payload: dict):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/status":
                    if not self._authorized():
                        self._send_json(403, {"error": "forbidden"})
                        return
                    self._send_json(200, controller.status())
                elif self.path in ("/power/on", "/power/off"):
                    self._send_json(405, {"error": "method not allowed"})
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self):
                if self.path == "/power/on":
                    if not self._authorized():
                        self._send_json(403, {"error": "forbidden"})
                        return
                    # A failed power change (e.g. firewall error) must never
                    # be reported as a successful transition: 500 + error.
                    result = controller.manual_power_on()
                    self._send_json(500 if "error" in result else 200, result)
                elif self.path == "/power/off":
                    if not self._authorized():
                        self._send_json(403, {"error": "forbidden"})
                        return
                    result = controller.manual_power_off()
                    self._send_json(500 if "error" in result else 200, result)
                elif self.path == "/status":
                    self._send_json(405, {"error": "method not allowed"})
                else:
                    self._send_json(404, {"error": "not found"})

            def log_message(self, fmt, *args):
                # Keep control traffic quiet; events are logged by the controller.
                pass

        heartbeat = self._heartbeat

        class MonitoredServer(ThreadingHTTPServer):
            # serve_forever() calls service_actions() once per select cycle,
            # so overriding it proves the serving thread is still running
            # even when no requests are in flight.
            def service_actions(self):
                super().service_actions()
                heartbeat.beat()

        self._server = MonitoredServer(("0.0.0.0", port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._serve,
            args=(self._server,),
            name="control-http",
            daemon=True,
        )
        self._thread.start()
        print(f"Control API listening on 0.0.0.0:{port}", flush=True)

    def _serve(self, server):
        _set_kernel_thread_name("control-http")
        server.serve_forever(poll_interval=0.5)

    def is_alive(self) -> bool:
        """True while the serve_forever thread is still running."""
        return self._thread is not None and self._thread.is_alive()

    def heartbeat_stale(self) -> bool:
        """True if the serving thread stopped beating (killed or wedged)."""
        return self._heartbeat.stale()

    # How long stop() waits for the serve_forever loop to exit before
    # giving up. BaseServer.shutdown() blocks until that loop's finally
    # block runs; a serving thread killed by the kernel (SIGKILL to its
    # TID) or wedged forever never runs it, and an unbounded wait here
    # would hide the very failure the agent is trying to report on the
    # fatal path.
    _STOP_TIMEOUT = 5.0

    def stop(self):
        if self._server is None:
            return
        server = self._server
        self._server = None
        # Run shutdown() in a side thread so the wait is bounded: if the
        # serving thread is dead or wedged the shutdown event is never set
        # and the waiter is simply abandoned (daemon); the listening socket
        # is closed either way.
        waiter = threading.Thread(target=server.shutdown, daemon=True)
        waiter.start()
        waiter.join(self._STOP_TIMEOUT)
        server.server_close()


# ---------------------------------------------------------------------------
# Worker watchdog
# ---------------------------------------------------------------------------

# How long a mandatory worker thread may go without a heartbeat before the
# main loop treats it as dead. Generous enough to survive heavy host load
# (and brief test-side SIGSTOP windows), short enough that a dead component
# cannot keep posing as a healthy device for long.
HEARTBEAT_TIMEOUT = 10.0


def _set_kernel_thread_name(name: str):
    """Set the kernel-visible thread name (comm) for external tooling.

    Python's ``Thread.name`` is invisible to the kernel (``top -H``,
    /proc, py-spy all show "python3"). Writing /proc/thread-self/comm
    from inside the thread makes each worker identifiable by name from
    the outside (used by the integration test to target a single
    thread). Best-effort: never fail the agent over this. Must be
    called from the thread being named.
    """
    try:
        with open("/proc/thread-self/comm", "w") as f:
            f.write(name[:15])
    except OSError:
        pass


class WorkerHeartbeat:
    """Detects worker-thread death that Python cannot see.

    ``threading.Thread.is_alive()`` only flips when the thread's target
    function returns or raises. A thread the kernel kills directly (SIGKILL
    to its TID) or a thread wedged forever never runs any more Python, so
    ``is_alive()`` stays True forever. Each worker therefore beats this
    object from inside its own loop; a stale beat means the component is
    dead or wedged even though the Thread object still claims to be alive.
    """

    def __init__(self):
        self._stamp = time.monotonic()

    def beat(self):
        self._stamp = time.monotonic()

    def stale(self, timeout: float = HEARTBEAT_TIMEOUT) -> bool:
        return (time.monotonic() - self._stamp) > timeout


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_main_loop(stop_event: threading.Event, data: SNMPDataStore,
                  power: PowerController, control: ControlHTTPServer,
                  snmp_handler: SNMPUDPHandler) -> str | None:
    """Run the main loop until *stop_event* is set.

    Returns a fatal-error message if a mandatory component thread died while
    running (the caller must exit non-zero so the container does not keep
    presenting as a healthy device), or None for a clean stop.
    """
    fatal_error = None
    last_counter_update = time.time()
    last_scheduler_advance = time.time()

    while not stop_event.is_set():
        time.sleep(0.5)
        now = time.time()

        # Post-startup failure detection: both mandatory components must
        # stay alive AND keep working. is_alive() alone misses a thread
        # killed outside Python (SIGKILL to its TID) or wedged forever,
        # so each worker also has to keep beating its heartbeat.
        if not control.is_alive():
            fatal_error = "Control API thread terminated unexpectedly"
            break
        if control.heartbeat_stale():
            fatal_error = "Control API thread stopped responding"
            break
        if not snmp_handler.is_alive():
            fatal_error = "SNMP thread terminated unexpectedly"
            break
        if snmp_handler.heartbeat_stale():
            fatal_error = "SNMP thread stopped responding"
            break

        # Update traffic counters
        if now - last_counter_update > 1.0:
            data.update_counters()
            last_counter_update = now

        # Advance outage scheduler; route transitions through the controller
        # so manual power commands and scheduler events cannot race.
        if data.outage_scheduler:
            if now - last_scheduler_advance > 1.0:
                try:
                    events = data.outage_scheduler.advance(now)
                    for event in events:
                        if event == "power_off":
                            power.scheduler_power_off()
                        elif event == "power_on":
                            power.scheduler_power_on()
                except Exception as exc:
                    log_event(power.simulation_name, power.device_id,
                              f"Scheduler error: {exc}")
                last_scheduler_advance = now

    return fatal_error


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    config_path = os.environ.get("AGENT_CONFIG", "/etc/network-swarm/config.json")

    if not os.path.exists(config_path):
        print(f"Error: Agent config not found at {config_path}", flush=True)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    device_id = config["device_id"]
    simulation_name = config.get("simulation_name", "unknown-simulation")

    data = SNMPDataStore(config)
    log_event(simulation_name, device_id, "Starting agent")

    snmp_port = config.get("snmp_port", 161)
    snmp_handler = SNMPUDPHandler(data, port=snmp_port)

    try:
        snmp_handler.start()
    except Exception as exc:
        log_event(simulation_name, device_id, f"SNMP startup failure: {exc}")
        raise

    control_port = int(config.get("control_port", CONTROL_PORT))
    power = PowerController(
        data,
        device_id=device_id,
        device_name=data.sys_name,
        device_ip=data.ip,
        simulation_name=simulation_name,
        control_port=control_port,
    )

    control = ControlHTTPServer(power, management_subnet=config.get("management_subnet"))
    try:
        control.start(port=control_port)
    except Exception as exc:
        # The control API is a mandatory component: without it the device
        # cannot be managed (HTTP power control, /status). Fail fatally
        # instead of running as a partially functional device.
        log_event(simulation_name, device_id,
                  f"Control API startup failure on port {control_port}: {exc}")
        print(f"Error: control API startup failure on port {control_port}: {exc}",
              file=sys.stderr, flush=True)
        snmp_handler.stop()
        sys.exit(1)

    # The outage firewall is a mandatory component: verify that we can
    # manage the SNMPSwarm-owned iptables chains before accepting traffic.
    if not power.firewall_preflight():
        print("Error: firewall preflight failed", file=sys.stderr, flush=True)
        control.stop()
        snmp_handler.stop()
        sys.exit(1)

    # SIGTERM/SIGINT are for graceful process termination only. Power control
    # is handled exclusively by the HTTP control API.
    stop_event = threading.Event()

    def _handle_shutdown(signum, frame):
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = str(signum)
        log_event(simulation_name, device_id, f"Received {sig_name}; shutting down gracefully")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Run outage scheduler loop
    if data.outage_scheduler:
        log_event(simulation_name, device_id, "Outage scheduler enabled")

    _set_kernel_thread_name("agent-main")
    fatal_error = run_main_loop(stop_event, data, power, control, snmp_handler)

    # Remove every SNMPSwarm-owned firewall rule/chain on termination.
    power.cleanup_firewall()
    control.stop()
    snmp_handler.stop()

    if fatal_error is not None:
        log_event(simulation_name, device_id,
                  f"{fatal_error}; exiting with failure")
        print(f"Error: {fatal_error}", file=sys.stderr, flush=True)
        sys.exit(1)

    log_event(simulation_name, device_id, "Agent stopped")


if __name__ == "__main__":
    main()
