import unittest
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "zigbee-monitor"))

from fastapi.testclient import TestClient
from app import app


class TestApiEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_status_endpoint(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("status", data)
        if data["status"] != "no_data":
            self.assertIn("snapshot", data)
            self.assertIn("total_devices", data["snapshot"])
            self.assertIn("online_count", data["snapshot"])
        else:
            self.assertIn("anomalies", data)


    def test_devices_endpoint(self):
        response = self.client.get("/api/devices")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("devices", data)
        self.assertIsInstance(data["devices"], list)

    def test_technician_export(self):
        response = self.client.get("/api/export/technician")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers.get("content-type", ""))

    def test_topology_scan_status(self):
        response = self.client.get("/api/topology/scan/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("is_scanning", data)

    def test_replacements_endpoint(self):
        response = self.client.get("/api/replacements")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("replacements", data)
        self.assertIsInstance(data["replacements"], list)

    def test_blocklist_endpoint(self):
        response = self.client.get("/api/blocklist")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("blocklist", data)
        self.assertIsInstance(data["blocklist"], list)


if __name__ == "__main__":
    unittest.main()
