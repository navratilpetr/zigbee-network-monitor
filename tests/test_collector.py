import unittest
import sys
import os
import json
from datetime import datetime, timezone
from pathlib import Path

# Add zigbee-monitor to sys.path
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "zigbee-monitor"))

from collector import parse_iso_time, ZigbeeCollector


class TestCollectorHelpers(unittest.TestCase):
    def test_parse_iso_time_valid(self):
        ts = "2026-10-13T10:15:30.123+02:00"
        dt = parse_iso_time(ts)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 13)

    def test_parse_iso_time_with_z(self):
        ts = "2026-10-13T08:15:30.123Z"
        dt = parse_iso_time(ts)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.hour, 8)

    def test_parse_iso_time_invalid_or_empty(self):
        dt_empty = parse_iso_time("")
        self.assertEqual(dt_empty, datetime.min.replace(tzinfo=timezone.utc))

        dt_invalid = parse_iso_time("invalid-date-string")
        self.assertEqual(dt_invalid, datetime.min.replace(tzinfo=timezone.utc))

    def test_collector_custom_config_override(self):
        custom_config = {
            "offline_threshold_hours": 24,
            "lqi_weak_threshold": 40,
            "battery_low_threshold": 15
        }
        collector = ZigbeeCollector(config=custom_config)
        self.assertEqual(collector.config["offline_threshold_hours"], 24)
        self.assertEqual(collector.config["lqi_weak_threshold"], 40)
        self.assertEqual(collector.config["battery_low_threshold"], 15)
        self.assertEqual(collector.config["retention_days"], 120)

    def test_purge_old_history_runs_cleanly(self):
        from database import purge_old_history
        result = purge_old_history(retention_days=120)
        self.assertIn("history_deleted", result)
        self.assertIn("snapshots_deleted", result)
        self.assertIn("events_deleted", result)

    def test_compute_network_topology_parents_with_scan_timeout_persistence(self):
        collector = ZigbeeCollector()
        devices = [
            {"ieeeAddr": "0xcoord", "type": "Coordinator"},
            {"ieeeAddr": "0xrouter1", "type": "Router"},
            {"ieeeAddr": "0xvalve_211hll", "type": "EndDevice"},
        ]
        online_status = {
            "0xcoord": True,
            "0xrouter1": True,
            "0xvalve_211hll": True,
        }
        # In current scan, 0xrouter1 timed out and 0xvalve_211hll has no link
        links = [
            {"sourceIeeeAddr": "0xcoord", "targetIeeeAddr": "0xrouter1", "relationship": 0, "lqi": 150}
        ]
        existing_parents = {
            "0xvalve_211hll": "0xrouter1"
        }

        # With existing_parents, the EndDevice should preserve its last known parent
        parents = collector.compute_network_topology_parents(
            devices_raw=devices,
            links=links,
            online_status=online_status,
            existing_parents=existing_parents
        )
        self.assertEqual(parents.get("0xvalve_211hll"), "0xrouter1")
        self.assertEqual(parents.get("0xrouter1"), "0xcoord")


    def test_auto_extract_mqtt_config_from_z2m_configuration(self):
        collector = ZigbeeCollector()
        collector.config["mqtt_user"] = ""
        collector.config["mqtt_password"] = ""
        
        cfg = {
            "mqtt": {
                "base_topic": "zigbee2mqtt",
                "server": "mqtt://172.30.32.1:1883",
                "user": "mqttuser",
                "password": "secret_password"
            }
        }
        collector._extract_mqtt_config_if_needed(cfg)
        self.assertEqual(collector.config["mqtt_user"], "mqttuser")
        self.assertEqual(collector.config["mqtt_password"], "secret_password")
        self.assertEqual(collector.config["mqtt_host"], "172.30.32.1")
        self.assertEqual(collector.config["mqtt_port"], 1883)


if __name__ == "__main__":
    unittest.main()
