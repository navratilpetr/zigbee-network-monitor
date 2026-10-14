import unittest
import sys
import os
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add zigbee-monitor to sys.path
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "zigbee-monitor"))

from database import (
    init_db, get_connection, record_device_replacement,
    get_device_replacements, sync_blocklist, get_blocklist
)
from collector import ZigbeeCollector


class TestHardwareLifecycleAndForensicAudit(unittest.TestCase):
    def setUp(self):
        # Use an isolated test database
        self.test_db_path = PROJECT_DIR / "zigbee-monitor" / "data" / "test_lifecycle.db"
        if self.test_db_path.exists():
            self.test_db_path.unlink()

        import database
        self.original_db_path = database.DB_PATH
        database.DB_PATH = self.test_db_path
        init_db()

    def tearDown(self):
        import database
        database.DB_PATH = self.original_db_path
        if self.test_db_path.exists():
            self.test_db_path.unlink()

    def test_record_and_get_device_replacements(self):
        rep_id = record_device_replacement(
            friendly_name="105HL",
            old_ieee="0x08ddebfffef11111",
            new_ieee="0x08ddebfffef22222",
            reason="Vadný servomotor",
            details="Výměna kus za kus"
        )
        self.assertGreater(rep_id, 0)

        # Retrieve all
        all_reps = get_device_replacements(friendly_name="105HL")
        self.assertEqual(len(all_reps), 1)
        self.assertEqual(all_reps[0]["friendly_name"], "105HL")
        self.assertEqual(all_reps[0]["old_ieee"], "0x08ddebfffef11111")
        self.assertEqual(all_reps[0]["new_ieee"], "0x08ddebfffef22222")

        # Filter by other
        empty = get_device_replacements(friendly_name="999HL_NEEXISTUJE")
        self.assertEqual(len(empty), 0)

    def test_sync_and_get_blocklist(self):
        blocklist = ["0xc09b9efffe4b47b9", "0xbaddeadbeef00001"]
        names = {"0xc09b9efffe4b47b9": "Vadna_Zasuvka"}
        sync_blocklist(blocklist, names)

        items = get_blocklist()
        self.assertGreaterEqual(len(items), 2)
        ieees = {x["ieee_addr"] for x in items}
        self.assertIn("0xc09b9efffe4b47b9", ieees)
        self.assertIn("0xbaddeadbeef00001", ieees)

        # Test removing one item
        sync_blocklist(["0xbaddeadbeef00001"], names)
        items2 = get_blocklist()
        ieees2 = {x["ieee_addr"] for x in items2}
        self.assertNotIn("0xc09b9efffe4b47b9", ieees2)
        self.assertIn("0xbaddeadbeef00001", ieees2)

        # Test emptying blocklist
        sync_blocklist([], names)
        self.assertEqual(len(get_blocklist()), 0)


    def test_collector_replacement_detection(self):
        collector = ZigbeeCollector()

        # Mock initial network with old device
        mock_devices_step1 = [
            {"ieeeAddr": "0x1111111111111111", "type": "EndDevice", "modelId": "TRVZB"}
        ]
        mock_states = {"0x1111111111111111": {"linkquality": 100, "battery": 90, "last_seen": "2026-10-13T12:00:00Z"}}
        mock_names_step1 = {"0x1111111111111111": "105HL"}

        with patch.object(collector, "fetch_raw_data", return_value=(mock_devices_step1, mock_states, mock_names_step1, [], "", {})):
            with patch.object(collector, "fetch_network_links", return_value=([], None)):
                summary1 = collector.run_collection()
                self.assertEqual(summary1["total_devices"], 1)

        # Step 2: Old device is removed and replaced by new IEEE with SAME friendly name
        mock_devices_step2 = [
            {"ieeeAddr": "0x2222222222222222", "type": "EndDevice", "modelId": "TRVZB"}
        ]
        mock_names_step2 = {"0x2222222222222222": "105HL"}
        mock_states_step2 = {"0x2222222222222222": {"linkquality": 120, "battery": 100, "last_seen": "2026-10-13T12:05:00Z"}}

        with patch.object(collector, "fetch_raw_data", return_value=(mock_devices_step2, mock_states_step2, mock_names_step2, [], "", {})):
            with patch.object(collector, "fetch_network_links", return_value=([], None)):
                summary2 = collector.run_collection()

        # Verify replacement was detected
        reps = get_device_replacements(friendly_name="105HL")
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0]["old_ieee"], "0x1111111111111111")
        self.assertEqual(reps[0]["new_ieee"], "0x2222222222222222")

        # Verify audit_events table contains DEVICE_REPLACED event
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT event_type, ieee_addr, friendly_name, severity FROM audit_events WHERE event_type = 'DEVICE_REPLACED' AND friendly_name = '105HL'")
        row = c.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["friendly_name"], "105HL")
        self.assertEqual(row["severity"], "warning")
        conn.close()

    def test_collector_blocklist_alert(self):
        collector = ZigbeeCollector()
        blocked_ieee = "0xc09b9efffe4b47b9"

        mock_devices = [
            {"ieeeAddr": blocked_ieee, "type": "Router", "modelId": "TS011F"}
        ]
        mock_states = {blocked_ieee: {"linkquality": 80, "last_seen": "2026-10-13T12:00:00Z"}}
        mock_names = {blocked_ieee: "Zavadna_Zasuvka_Stara"}

        with patch.object(collector, "fetch_raw_data", return_value=(mock_devices, mock_states, mock_names, [blocked_ieee], "", {})):
            with patch.object(collector, "fetch_network_links", return_value=([], None)):
                collector.run_collection()

        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT event_type, ieee_addr, severity FROM audit_events WHERE event_type = 'BLOCKED_DEVICE_DETECTED'")
        row = c.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["ieee_addr"], blocked_ieee)
        self.assertEqual(row["severity"], "danger")
        conn.close()

    def test_coordinator_conflict_resolution(self):
        collector = ZigbeeCollector()
        mock_log = (
            "[2026-10-14 10:48:37] warning: zh:ember:ezsp: An ID conflict was detected for network address '9732'. Corresponding devices kicked from the network.\n"
            "[2026-10-14 10:48:37] info: z2m:mqtt: MQTT publish: topic 'zigbee2mqtt/bridge/event', payload '{\"data\":{\"friendly_name\":\"C2\"},\"type\":\"device_announce\"}'\n"
        )
        with patch.object(collector, "fetch_raw_data", return_value=([], {}, {}, [], mock_log, {})):
            with patch.object(collector, "fetch_network_links", return_value=([], None)):
                collector.run_collection()

        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT event_type, severity FROM audit_events WHERE event_type = 'DEVICE_KICKED'")
        kicked = c.fetchone()
        self.assertIsNotNone(kicked)
        self.assertEqual(kicked["severity"], "warning")

        c.execute("SELECT event_type, severity FROM audit_events WHERE event_type = 'CONFLICT_RESOLVED'")
        resolved = c.fetchone()
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["severity"], "success")
        conn.close()


if __name__ == "__main__":
    unittest.main()
