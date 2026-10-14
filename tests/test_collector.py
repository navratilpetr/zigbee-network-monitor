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


if __name__ == "__main__":
    unittest.main()
