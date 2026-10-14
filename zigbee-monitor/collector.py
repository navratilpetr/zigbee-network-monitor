import os
import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Tuple, Optional
import re
import yaml
from zoneinfo import ZoneInfo

from database import (
    get_connection, purge_old_history, record_device_replacement,
    sync_blocklist, sync_orphaned_devices, set_last_radio_scan_timestamp,
    get_last_radio_scan_timestamp
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

import glob
import paho.mqtt.publish as mqtt_publish
import paho.mqtt.client as mqtt_client
try:
    from paho.mqtt.enums import CallbackAPIVersion
    _PAHO_V2 = True
except Exception:
    _PAHO_V2 = False

DEFAULT_CONFIG = {
    "source_mode": os.getenv("SOURCE_MODE", "auto"),
    "z2m_path": os.getenv("Z2M_PATH", "/config/zigbee2mqtt"),
    "mqtt_host": os.getenv("MQTT_HOST", "core-mosquitto"),
    "mqtt_port": int(os.getenv("MQTT_PORT", "1883")),
    "mqtt_user": os.getenv("MQTT_USER", ""),
    "mqtt_password": os.getenv("MQTT_PASSWORD", ""),
    "mqtt_topic": os.getenv("MQTT_TOPIC", "zigbee2mqtt"),
    "ssh_host": os.getenv("HA_SSH_HOST", ""),
    "ssh_port": int(os.getenv("HA_SSH_PORT", "22222")),
    "ssh_user": os.getenv("HA_SSH_USER", "hassio"),
    "ssh_key": os.getenv("HA_SSH_KEY", "/root/.ssh/id_ed25519"),
    "offline_threshold_hours": int(os.getenv("OFFLINE_THRESHOLD_HOURS", "12")),
    "lqi_weak_threshold": int(os.getenv("LQI_WEAK_THRESHOLD", "50")),
    "battery_low_threshold": int(os.getenv("BATTERY_LOW_THRESHOLD", "20")),
    "retention_days": int(os.getenv("RETENTION_DAYS", "120"))
}

def parse_iso_time(ts_str: str) -> datetime:
    if not ts_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        # Handles 2026-10-12T07:43:16.759Z
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)

class ZigbeeCollector:
    _last_auto_scan_time: float = 0.0
    _auto_scan_lock = threading.Lock()
    _active_scan_running: bool = False
    _active_scan_started_at: float = 0.0
    _active_scan_lock = threading.Lock()

    def __init__(self, config: Dict[str, Any] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.mode = self._determine_mode()
        self._mqtt_last_check: float = 0.0
        self._mqtt_last_status: Dict[str, Any] = {"status": "unknown", "message": "Inicializace"}
        self._auto_load_mqtt_config()
        logger.info(f"ZigbeeCollector initialized in '{self.mode}' mode. (Z2M Path: {self.config['z2m_path']})")

    def _determine_mode(self) -> str:
        # Auto-detect fallback paths if configured path does not exist
        if not os.path.isdir(self.config["z2m_path"]):
            for fallback in ("/homeassistant/zigbee2mqtt", "/config/zigbee2mqtt"):
                if os.path.isdir(fallback):
                    self.config["z2m_path"] = fallback
                    break

        explicit_mode = self.config.get("source_mode", "auto")
        if explicit_mode in ("local", "ssh", "mqtt"):
            return explicit_mode
        # Auto-detect: if local z2m directory exists, use local
        if os.path.isdir(self.config["z2m_path"]):
            return "local"
        if self.config.get("ssh_host"):
            return "ssh"
        return "local"

    def _extract_mqtt_config_if_needed(self, cfg: dict):
        if not isinstance(cfg, dict):
            return
        mqtt_sec = cfg.get("mqtt")
        if isinstance(mqtt_sec, dict):
            if not self.config.get("mqtt_user") and mqtt_sec.get("user"):
                self.config["mqtt_user"] = str(mqtt_sec.get("user"))
                logger.info(f"Auto-configured MQTT user from Z2M configuration: {self.config['mqtt_user']}")
            if not self.config.get("mqtt_password") and mqtt_sec.get("password"):
                self.config["mqtt_password"] = str(mqtt_sec.get("password"))
                logger.info("Auto-configured MQTT password from Z2M configuration.")
            if mqtt_sec.get("base_topic"):
                self.config["mqtt_topic"] = str(mqtt_sec.get("base_topic"))
            
            srv = mqtt_sec.get("server", "")
            if srv and (not self.config.get("mqtt_host") or self.config.get("mqtt_host") == "core-mosquitto"):
                m = re.search(r"mqtts?://([^:/]+)(?::([0-9]+))?", srv)
                if m:
                    self.config["mqtt_host"] = m.group(1)
                    if m.group(2):
                        self.config["mqtt_port"] = int(m.group(2))
                    logger.info(f"Auto-configured MQTT host from Z2M configuration: {self.config['mqtt_host']}:{self.config.get('mqtt_port')}")

    def _auto_load_mqtt_config(self):
        """Attempts to load and auto-configure MQTT credentials from configuration.yaml on startup."""
        cfg_file = os.path.join(self.config.get("z2m_path", ""), "configuration.yaml")
        if os.path.isfile(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8", errors="ignore") as f:
                    cfg = yaml.safe_load(f)
                    if isinstance(cfg, dict):
                        self._extract_mqtt_config_if_needed(cfg)
            except Exception as e:
                logger.debug(f"Could not preload configuration.yaml for MQTT on startup: {e}")

    def test_mqtt_connection(self) -> Tuple[bool, str]:
        """Tests whether connection to the MQTT broker succeeds."""
        self._auto_load_mqtt_config()
        if self.mode == "ssh" and self.config.get("ssh_host"):
            user = self.config.get("mqtt_user", "")
            pwd = self.config.get("mqtt_password", "")
            host = self.config.get("mqtt_host", "172.30.32.1")
            auth_args = f"-u {user} -P '{pwd}'" if user else ""
            topic = f"{self.config.get('mqtt_topic', 'zigbee2mqtt')}/bridge/request/health_check"
            cmd_test = f"mosquitto_pub -h {host} {auth_args} -t '{topic}' -m '{{}}' 2>&1"
            try:
                res = self._run_ssh(cmd_test)
                if "Error" in res or "Connection Refused" in res:
                    return False, res.strip()
                return True, "OK"
            except Exception as e:
                return False, str(e)
        else:
            try:
                if _PAHO_V2:
                    client = mqtt_client.Client(CallbackAPIVersion.VERSION2, client_id="zigbee_mon_probe")
                else:
                    client = mqtt_client.Client(client_id="zigbee_mon_probe")
            except Exception:
                client = mqtt_client.Client(client_id="zigbee_mon_probe")

            if self.config.get("mqtt_user"):
                client.username_pw_set(self.config["mqtt_user"], self.config.get("mqtt_password", ""))

            connected = False
            conn_err = None

            def on_connect(c, userdata, flags, rc, *args):
                nonlocal connected, conn_err
                code = getattr(rc, "value", rc)
                if code == 0:
                    connected = True
                elif code in (4, 134, 135):
                    conn_err = "Chybné jméno nebo heslo k MQTT brokeru"
                elif code in (5, 135):
                    conn_err = "Přístup k MQTT brokeru odmítnut (Not authorized)"
                else:
                    conn_err = f"Chyba spojení s MQTT brokerem (kód {code})"

            client.on_connect = on_connect
            hosts_to_try = [self.config.get("mqtt_host", "core-mosquitto")]
            if "172.30.32.1" not in hosts_to_try:
                hosts_to_try.append("172.30.32.1")

            last_ex = None
            for h in hosts_to_try:
                try:
                    port = int(self.config.get("mqtt_port", 1883))
                    client.connect(h, port, keepalive=3)
                    client.loop_start()
                    start_t = time.time()
                    while time.time() - start_t < 2.0:
                        if connected or conn_err:
                            break
                        time.sleep(0.1)
                    client.loop_stop()
                    try:
                        client.disconnect()
                    except Exception:
                        pass

                    if connected:
                        self.config["mqtt_host"] = h
                        return True, "OK"
                    if conn_err:
                        return False, conn_err
                except Exception as ex:
                    last_ex = ex

            return False, conn_err or str(last_ex or "Nelze se spojit s MQTT brokerem")

    def get_communication_status(self) -> Dict[str, Any]:
        """Returns health status of Z2M data source and MQTT communication."""
        z2m_ok = False
        z2m_msg = ""
        z2m_dir = self.config.get("z2m_path", "")
        if self.mode == "local":
            db_p = os.path.join(z2m_dir, "database.db")
            if os.path.isfile(db_p):
                z2m_ok = True
                z2m_msg = "Soubory Zigbee2MQTT jsou čteny lokálně v pořádku"
            else:
                z2m_msg = f"Nenalezen soubor database.db v {z2m_dir}"
        elif self.mode == "ssh":
            if self.config.get("ssh_host"):
                z2m_ok = True
                z2m_msg = f"SSH spojení na {self.config.get('ssh_host')} aktivní"
            else:
                z2m_msg = "SSH hostitel není nakonfigurován"
        else:
            z2m_ok = True
            z2m_msg = "MQTT režim aktivní"

        now = time.time()
        if now - getattr(self, "_mqtt_last_check", 0) > 60:
            ok, msg = self.test_mqtt_connection()
            self._mqtt_last_check = now
            self._mqtt_last_status = {
                "status": "ok" if ok else "error",
                "message": msg,
                "host": self.config.get("mqtt_host", "core-mosquitto"),
                "port": self.config.get("mqtt_port", 1883),
                "user": self.config.get("mqtt_user") or None
            }

        return {
            "z2m": {
                "status": "ok" if z2m_ok else "error",
                "mode": self.mode,
                "path": z2m_dir,
                "message": z2m_msg
            },
            "mqtt": getattr(self, "_mqtt_last_status", {"status": "unknown", "message": "Nekontrolováno"})
        }

    def _run_ssh(self, remote_command: str) -> str:
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-i", self.config["ssh_key"],
            "-p", str(self.config["ssh_port"]),
            f"{self.config['ssh_user']}@{self.config['ssh_host']}",
            remote_command
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout

    def _fetch_raw_data_local(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], List[str], str, Dict[str, str]]:
        """Direct local filesystem read for Home Assistant Add-on or local Docker/LXC container."""
        z2m_dir = self.config["z2m_path"]

        # 1. Parse database.db
        devices = []
        db_file = os.path.join(z2m_dir, "database.db")
        if os.path.isfile(db_file):
            try:
                with open(db_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                devices.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
            except Exception as e:
                logger.warning(f"Error reading local database.db: {e}")

        # 2. Parse state.json
        states = {}
        state_file = os.path.join(z2m_dir, "state.json")
        if os.path.isfile(state_file):
            try:
                with open(state_file, "r", encoding="utf-8", errors="ignore") as f:
                    states = json.load(f)
            except Exception as e:
                logger.warning(f"Error reading local state.json: {e}")

        # 3. Parse configuration.yaml
        friendly_names = {}
        blocklist = []
        cfg_file = os.path.join(z2m_dir, "configuration.yaml")
        if os.path.isfile(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8", errors="ignore") as f:
                    cfg = yaml.safe_load(f)
                    if isinstance(cfg, dict):
                        if "devices" in cfg and isinstance(cfg["devices"], dict):
                            for ieee, dev_info in cfg["devices"].items():
                                if isinstance(dev_info, dict) and "friendly_name" in dev_info:
                                    friendly_names[ieee] = dev_info["friendly_name"]
                        if "blocklist" in cfg and isinstance(cfg["blocklist"], list):
                            blocklist = [str(x) for x in cfg["blocklist"]]
                        self._extract_mqtt_config_if_needed(cfg)
            except Exception as e:
                logger.warning(f"Error reading local configuration.yaml: {e}")

        # 4. Parse Home Assistant core.device_registry if available
        ha_mqtt_devices = {}
        ha_candidates = [
            "/config/.storage/core.device_registry",
            "/homeassistant/.storage/core.device_registry"
        ]
        for dev_reg_path in ha_candidates:
            if os.path.isfile(dev_reg_path):
                try:
                    with open(dev_reg_path, "r", encoding="utf-8", errors="ignore") as f:
                        ha_data = json.load(f)
                        for dev in ha_data.get("data", {}).get("devices", []):
                            for ident in dev.get("identifiers", []):
                                if len(ident) >= 2 and ident[0] == "mqtt" and str(ident[1]).startswith("zigbee2mqtt_"):
                                    ieee_part = str(ident[1]).replace("zigbee2mqtt_", "")
                                    if ieee_part.startswith("0x"):
                                        dev_name = dev.get("name_by_user") or dev.get("name") or ieee_part
                                        ha_mqtt_devices[ieee_part] = dev_name
                    break
                except Exception as e:
                    logger.warning(f"Error reading local HA device registry: {e}")

        # 5. Live logs inspection
        log_raw = ""
        log_dirs = sorted(glob.glob(os.path.join(z2m_dir, "log", "*")), key=os.path.getmtime, reverse=True)
        if log_dirs:
            latest_dir = log_dirs[0]
            log_files = sorted(glob.glob(os.path.join(latest_dir, "*.log")), key=os.path.getmtime, reverse=True)
            lines = []
            for lf in log_files[:2]:
                try:
                    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
                        lines.extend(f.readlines())
                except Exception:
                    pass
            leave_lines = [l for l in lines[-1000:] if any(k in l for k in ["left the network", "kicked from the network", "device_leave", "device_announce", "Accepting joining", "ID conflict"])]
            recent_lines = [l for l in lines[-2500:] if not any(k in l for k in ["info:.*z2m:mqtt: MQTT publish:", "Failed to execute LQI for 'Zasuvka_", "OTA update of", "[NCP COUNTERS]", "[ASH COUNTERS]"])]
            log_raw = ("\n".join(leave_lines[-80:]) + "\n" + "\n".join(recent_lines[-120:])).strip()

        return devices, states, friendly_names, blocklist, log_raw, ha_mqtt_devices

    def _fetch_raw_data_ssh(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], List[str], str, Dict[str, str]]:
        """Non-invasive read of database.db, state.json, configuration.yaml, HA device_registry, and filtered log in a single SSH connection"""
        z2m_dir = self.config["z2m_path"]
        batch_script = (
            f"echo '===DATABASE_START==='; cat {z2m_dir}/database.db 2>/dev/null; "
            f"echo '===STATE_START==='; cat {z2m_dir}/state.json 2>/dev/null; "
            f"echo '===CONFIG_START==='; cat {z2m_dir}/configuration.yaml 2>/dev/null; "
            f"echo '===HA_DEVICES_START==='; cat /homeassistant/.storage/core.device_registry 2>/dev/null; "
            f"echo '===LOG_START==='; "
            f"LDIR=$(ls -td {z2m_dir}/log/* 2>/dev/null | head -n1); "
            f"if [ -n \"$LDIR\" ]; then "
            f"tail -q -n 2500 $(ls -t \"$LDIR\"/*.log 2>/dev/null | head -n2) 2>/dev/null | "
            f"grep -v -E \"info:.*z2m:mqtt: MQTT publish:|Failed to execute LQI for 'Zasuvka_|OTA update of .* at |\\[NCP COUNTERS\\]|\\[ASH COUNTERS\\]\" | tail -n 120; "
            f"fi; "
            f"echo '===LEAVE_EVENTS_START==='; "
            f"if [ -n \"$LDIR\" ]; then "
            f"grep -h -E \"left the network|kicked from the network|device_leave|device_announce|Accepting joining|ID conflict\" \"$LDIR\"/*.log 2>/dev/null | tail -n 80; "
            f"fi;"
        )
        raw_output = self._run_ssh(batch_script)

        parts = raw_output.split("===STATE_START===")
        db_raw = parts[0].replace("===DATABASE_START===", "").strip()
        state_and_rest = parts[1].split("===CONFIG_START===")
        state_raw = state_and_rest[0].strip()
        cfg_and_rest = state_and_rest[1].split("===HA_DEVICES_START===") if len(state_and_rest) > 1 else ["", ""]
        cfg_raw = cfg_and_rest[0].strip()
        ha_and_rest = cfg_and_rest[1].split("===LOG_START===") if len(cfg_and_rest) > 1 else ["", ""]
        ha_raw = ha_and_rest[0].strip()
        log_and_leave = ha_and_rest[1].split("===LEAVE_EVENTS_START===") if len(ha_and_rest) > 1 else ["", ""]
        recent_log_raw = log_and_leave[0].strip()
        leave_raw = log_and_leave[1].strip() if len(log_and_leave) > 1 else ""
        log_raw = (leave_raw + "\n" + recent_log_raw).strip()

        # Parse NDJSON database
        devices = []
        for line in db_raw.split("\n"):
            line = line.strip()
            if line:
                try:
                    devices.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # Parse state.json
        states = {}
        if state_raw:
            try:
                states = json.loads(state_raw)
            except json.JSONDecodeError:
                pass

        # Parse configuration.yaml for friendly names and blocklist
        friendly_names = {}
        blocklist = []
        if cfg_raw:
            try:
                cfg = yaml.safe_load(cfg_raw)
                if isinstance(cfg, dict):
                    if "devices" in cfg and isinstance(cfg["devices"], dict):
                        for ieee, dev_info in cfg["devices"].items():
                            if isinstance(dev_info, dict) and "friendly_name" in dev_info:
                                friendly_names[ieee] = dev_info["friendly_name"]
                    if "blocklist" in cfg and isinstance(cfg["blocklist"], list):
                        blocklist = [str(x) for x in cfg["blocklist"]]
                    self._extract_mqtt_config_if_needed(cfg)
            except Exception as e:
                logger.warning(f"Error parsing configuration.yaml: {e}")

        # Parse Home Assistant core.device_registry for MQTT registered devices
        ha_mqtt_devices = {}
        if ha_raw:
            try:
                ha_data = json.loads(ha_raw)
                for dev in ha_data.get("data", {}).get("devices", []):
                    for ident in dev.get("identifiers", []):
                        if len(ident) >= 2 and ident[0] == "mqtt" and str(ident[1]).startswith("zigbee2mqtt_"):
                            ieee_part = str(ident[1]).replace("zigbee2mqtt_", "")
                            if ieee_part.startswith("0x"):
                                dev_name = dev.get("name_by_user") or dev.get("name") or ieee_part
                                ha_mqtt_devices[ieee_part] = dev_name
            except Exception as e:
                logger.warning(f"Error parsing HA device registry: {e}")

        return devices, states, friendly_names, blocklist, log_raw, ha_mqtt_devices

    def fetch_raw_data(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], List[str], str, Dict[str, str]]:
        if self.mode == "local":
            return self._fetch_raw_data_local()
        return self._fetch_raw_data_ssh()

    def compute_network_topology_parents(
        self,
        devices_raw: List[Dict[str, Any]],
        links: List[Dict[str, Any]],
        online_status: Dict[str, bool],
        existing_parents: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """
        Builds a comprehensive spanning tree connecting every online device to the Coordinator.
        - For Routers: Uses Dijkstra/BFS from Coordinator across mesh links (shortest hops, highest LQI).
        - For EndDevices: Uses reported parent (rel 0/1) or closest active neighbor router.
        - Persistence: If an online device has no links in current scan (e.g. parent router timed out during scan),
          it retains its last known parent from the database so topology links don't get lost.
        """
        import heapq
        
        dev_map = {d.get("ieeeAddr"): d for d in devices_raw if d.get("ieeeAddr")}
        coord_ieee = None
        for ieee, d in dev_map.items():
            if d.get("type") == "Coordinator":
                coord_ieee = ieee
                break
        
        if not coord_ieee:
            return {}

        # Build adjacency graph
        adj = {}
        for l in links:
            s = l.get("sourceIeeeAddr")
            t = l.get("targetIeeeAddr")
            lqi = l.get("lqi") or l.get("linkquality") or 1
            rel = l.get("relationship", 2)
            if s and t:
                adj.setdefault(s, []).append((t, lqi, rel))
                adj.setdefault(t, []).append((s, lqi, rel))

        # 1. Dijkstra spanning tree for all routers from Coordinator
        dist = {coord_ieee: 0}
        router_parents = {}
        pq = [(0, 0, coord_ieee)]  # (hops, -lqi, u)
        visited = set()

        while pq:
            hops, neg_lqi, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)

            for v, lqi, rel in adj.get(u, []):
                v_dev = dev_map.get(v)
                if not v_dev or not online_status.get(v, False):
                    continue
                if v_dev.get("type") in ("Router", "Coordinator"):
                    new_hops = hops + 1
                    cost = new_hops * 1000 - lqi
                    if v not in dist or cost < dist[v]:
                        dist[v] = cost
                        router_parents[v] = u
                        heapq.heappush(pq, (new_hops, -lqi, v))

        # 2. EndDevice parents from rel 0 or 1
        end_parents = {}
        for l in links:
            rel = l.get("relationship")
            s = l.get("sourceIeeeAddr")
            t = l.get("targetIeeeAddr")
            if rel == 0 and s and t and dev_map.get(t, {}).get("type") == "EndDevice":
                end_parents[t] = s
            elif rel == 1 and s and t and dev_map.get(s, {}).get("type") == "EndDevice":
                end_parents[s] = t

        # Combine
        parents = {}
        for ieee, d in dev_map.items():
            if not online_status.get(ieee, False) or d.get("type") == "Coordinator":
                continue
            dtype = d.get("type")
            if dtype == "Router":
                p = router_parents.get(ieee)
                if p:
                    parents[ieee] = p
                elif existing_parents and existing_parents.get(ieee):
                    prev_p = existing_parents.get(ieee)
                    if prev_p in dev_map:
                        parents[ieee] = prev_p
            else:  # EndDevice
                p = end_parents.get(ieee)
                if p:
                    parents[ieee] = p
                else:
                    candidates = [
                        n for n in adj.get(ieee, []) 
                        if online_status.get(n[0], False) and dev_map.get(n[0], {}).get("type") in ("Router", "Coordinator")
                    ]
                    if candidates:
                        best = max(candidates, key=lambda x: x[1])
                        parents[ieee] = best[0]
                    elif existing_parents and existing_parents.get(ieee):
                        prev_p = existing_parents.get(ieee)
                        if prev_p in dev_map:
                            parents[ieee] = prev_p

        return parents

    def _fetch_network_links_local(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        z2m_dir = self.config["z2m_path"]
        best_line = None
        best_ts = None
        for f in glob.glob(os.path.join(z2m_dir, "log", "*", "log*.log")):
            try:
                with open(f, 'r', encoding="utf-8", errors='ignore') as fp:
                    for line in fp:
                        if 'response/networkmap' in line and 'payload' in line:
                            ts = line[1:20] if line.startswith('[') else None
                            if best_ts is None or (ts and ts > best_ts):
                                best_ts = ts
                                best_line = line
            except Exception:
                pass
        if best_line:
            iso_ts = None
            if best_ts:
                try:
                    dt = datetime.strptime(best_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Prague"))
                    iso_ts = dt.isoformat()
                except Exception:
                    pass
            try:
                payload_str = best_line.split('payload ', 1)[1].strip().strip("'")
                data = json.loads(payload_str)
                links = data.get('data', {}).get('value', {}).get('links', [])
                return links, iso_ts
            except Exception as e:
                logger.warning(f"Error parsing local networkmap payload: {e}")
        return [], None

    def _fetch_network_links_ssh(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        py_script = (
            "import json, glob\n"
            "best_line = None\n"
            "best_ts = None\n"
            "for f in glob.glob('/homeassistant/zigbee2mqtt/log/*/log*.log'):\n"
            "    with open(f, 'r', errors='ignore') as fp:\n"
            "        for line in fp:\n"
            "            if 'response/networkmap' in line and 'payload' in line:\n"
            "                ts = line[1:20] if line.startswith('[') else None\n"
            "                if best_ts is None or (ts and ts > best_ts):\n"
            "                    best_ts = ts\n"
            "                    best_line = line\n"
            "if best_line:\n"
            "    payload_str = best_line.split('payload ', 1)[1].strip().strip(\"'\")\n"
            "    data = json.loads(payload_str)\n"
            "    links = data.get('data', {}).get('value', {}).get('links', [])\n"
            "    print(json.dumps({'links': links, 'timestamp': best_ts}))\n"
            "else:\n"
            "    print(json.dumps({'links': [], 'timestamp': None}))\n"
        )
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-i", self.config["ssh_key"],
            "-p", str(self.config["ssh_port"]),
            f"{self.config['ssh_user']}@{self.config['ssh_host']}",
            "python3"
        ]
        try:
            res = subprocess.run(cmd, input=py_script, text=True, capture_output=True, check=True)
            out = json.loads(res.stdout.strip())
            links = out.get("links", [])
            ts_str = out.get("timestamp")
            iso_ts = None
            if ts_str:
                try:
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Prague"))
                    iso_ts = dt.isoformat()
                except Exception:
                    iso_ts = None
            return links, iso_ts
        except Exception as e:
            logger.warning(f"Failed to fetch networkmap links via SSH: {e}")
            return [], None

    def fetch_network_links(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Reads latest networkmap links from Z2M logs without extra radio queries.
        Returns (links, latest_scan_iso_timestamp).
        """
        if self.mode == "local":
            return self._fetch_network_links_local()
        return self._fetch_network_links_ssh()

    def get_active_scan_status(self) -> Tuple[bool, int]:
        """Returns (is_scanning, elapsed_seconds). Auto-recovers if stuck > 200s."""
        with ZigbeeCollector._active_scan_lock:
            if ZigbeeCollector._active_scan_running:
                elapsed = int(time.time() - ZigbeeCollector._active_scan_started_at)
                if elapsed > 200:
                    logger.warning("Active scan ran longer than 200s, auto-resetting status.")
                    ZigbeeCollector._active_scan_running = False
                    ZigbeeCollector._active_scan_started_at = 0.0
                    return False, 0
                return True, elapsed

        # Check if an external scan is running on Home Assistant directly
        ha_running, _ = self.is_active_map_scan_running()
        if ha_running:
            return True, 30
        return False, 0

    def _is_active_map_scan_running_local(self) -> Tuple[bool, str]:
        z2m_dir = self.config["z2m_path"]
        log_files = glob.glob(os.path.join(z2m_dir, "log", "*", "log*.log"))
        if not log_files:
            return False, ""
        latest_log = max(log_files, key=os.path.getmtime)
        try:
            with open(latest_log, 'r', encoding="utf-8", errors='ignore') as f:
                lines = f.readlines()[-250:]
            start_ts = None
            finish_ts = None
            for l in lines:
                if 'Starting network scan' in l:
                    m = re.search(r'\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\]', l)
                    if m:
                        try:
                            start_ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                        except Exception:
                            start_ts = time.time()
                    else:
                        start_ts = time.time()
                elif 'Network scan finished' in l:
                    m = re.search(r'\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\]', l)
                    if m:
                        try:
                            finish_ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
                        except Exception:
                            finish_ts = time.time()
                    else:
                        finish_ts = time.time()
            now = time.time()
            if start_ts is not None and (finish_ts is None or start_ts > finish_ts) and (now - start_ts < 240):
                return True, "Scan právě běží přímo v rozhraní Zigbee2MQTT."
        except Exception as e:
            logger.warning(f"Failed to check local scan status: {e}")
        return False, ""

    def _is_active_map_scan_running_ssh(self) -> Tuple[bool, str]:
        py_check = (
            "import glob, os, time, re\n"
            "from datetime import datetime\n"
            "log_files = glob.glob('/homeassistant/zigbee2mqtt/log/*/log*.log')\n"
            "if not log_files:\n"
            "    print('IDLE')\n"
            "    exit()\n"
            "latest_log = max(log_files, key=os.path.getmtime)\n"
            "with open(latest_log, 'r', errors='ignore') as f:\n"
            "    lines = f.readlines()[-250:]\n"
            "start_ts = None\n"
            "finish_ts = None\n"
            "for l in lines:\n"
            "    if 'Starting network scan' in l:\n"
            "        m = re.search(r'\\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\\]', l)\n"
            "        if m:\n"
            "            try:\n"
            "                start_ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()\n"
            "            except Exception:\n"
            "                start_ts = time.time()\n"
            "        else:\n"
            "            start_ts = time.time()\n"
            "    elif 'Network scan finished' in l:\n"
            "        m = re.search(r'\\[([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})\\]', l)\n"
            "        if m:\n"
            "            try:\n"
            "                finish_ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').timestamp()\n"
            "            except Exception:\n"
            "                finish_ts = time.time()\n"
            "        else:\n"
            "            finish_ts = time.time()\n"
            "now = time.time()\n"
            "if start_ts is not None and (finish_ts is None or start_ts > finish_ts) and (now - start_ts < 240):\n"
            "    print('ACTIVE_SCAN_RUNNING')\n"
            "else:\n"
            "    print('IDLE')\n"
        )
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-i", self.config["ssh_key"],
            "-p", str(self.config["ssh_port"]),
            f"{self.config['ssh_user']}@{self.config['ssh_host']}",
            "python3"
        ]
        try:
            res = subprocess.run(cmd, input=py_check, text=True, capture_output=True, check=True)
            if res.stdout.strip() == "ACTIVE_SCAN_RUNNING":
                return True, "Scan právě běží přímo na Home Assistantu (v rozhraní Zigbee2MQTT)."
        except Exception as e:
            logger.warning(f"Failed to check HA scan status via SSH: {e}")
        return False, ""

    def is_active_map_scan_running(self) -> Tuple[bool, str]:
        """Checks if a network map scan is currently in progress."""
        if self.mode == "local":
            return self._is_active_map_scan_running_local()
        return self._is_active_map_scan_running_ssh()

    def trigger_active_map_scan(self) -> Tuple[bool, str]:
        """Sends MQTT request for a fresh networkmap scan (used in night schedule or on demand).
        Returns (success: bool, message: str).
        """
        topic = f"{self.config.get('mqtt_topic', 'zigbee2mqtt')}/bridge/request/networkmap"
        payload = json.dumps({"type": "raw"})
        self._auto_load_mqtt_config()

        if self.mode in ("local", "mqtt") or not self.config.get("ssh_host"):
            auth = None
            if self.config.get("mqtt_user"):
                auth = {
                    "username": self.config["mqtt_user"],
                    "password": self.config.get("mqtt_password", "")
                }
            hosts = [self.config.get("mqtt_host", "core-mosquitto")]
            if "172.30.32.1" not in hosts:
                hosts.append("172.30.32.1")

            port = int(self.config.get("mqtt_port", 1883))
            last_err = None
            for h in hosts:
                try:
                    mqtt_publish.single(
                        topic,
                        payload=payload,
                        hostname=h,
                        port=port,
                        auth=auth,
                        client_id="zigbee_monitor"
                    )
                    self.config["mqtt_host"] = h
                    logger.info(f"Sent networkmap request to MQTT topic: {topic} (broker: {h})")
                    return True, "OK"
                except Exception as e:
                    last_err = str(e)

            logger.error(f"Failed to publish networkmap request via MQTT: {last_err}")
            return False, last_err or "Chyba spojení s MQTT brokerem"
        else:
            user = self.config.get("mqtt_user", "")
            pwd = self.config.get("mqtt_password", "")
            host = self.config.get("mqtt_host", "172.30.32.1")
            auth_args = f"-u {user} -P '{pwd}'" if user else ""
            cmd_pub = f"mosquitto_pub -h {host} {auth_args} -t '{topic}' -m '{payload}'"
            try:
                res = self._run_ssh(cmd_pub)
                if "Error" in res or "Connection Refused" in res:
                    return False, res.strip()
                return True, "OK"
            except Exception as e:
                return False, str(e)

    def trigger_auto_radio_scan_if_needed(self, new_device_names: List[str]):
        """Triggers an active network map scan on Home Assistant when new devices are discovered, protected by 15-min cooldown."""
        with ZigbeeCollector._auto_scan_lock:
            now_sec = time.time()
            cooldown_sec = 15 * 60  # 15 minutes
            if now_sec - ZigbeeCollector._last_auto_scan_time < cooldown_sec:
                remaining_m = int((cooldown_sec - (now_sec - ZigbeeCollector._last_auto_scan_time)) / 60)
                logger.info(f"Auto radio scan skipped: cooldown active ({remaining_m} min remaining).")
                return False

            # Check if scan is already running locally or on HA
            is_sc, _ = self.get_active_scan_status()
            if is_sc:
                logger.info("Auto radio scan skipped: scan is already running.")
                return False

            ZigbeeCollector._last_auto_scan_time = now_sec

        def _scan_worker():
            with ZigbeeCollector._active_scan_lock:
                ZigbeeCollector._active_scan_running = True
                ZigbeeCollector._active_scan_started_at = time.time()
            try:
                logger.info(f"Auto radio scan triggered for new devices: {', '.join(new_device_names)}")
                self.trigger_active_map_scan()
                time.sleep(15)
                start_w = time.time()
                while time.time() - start_w < 180:
                    time.sleep(5)
                    ha_run, _ = self.is_active_map_scan_running()
                    if not ha_run:
                        logger.info("Active networkmap scan detected as completed on HA.")
                        break
                # Re-run passive collection to ingest new networkmap
                self.run_collection()
                set_last_radio_scan_timestamp(datetime.now(timezone.utc).isoformat())
                logger.info("Auto radio scan and subsequent collection completed successfully.")
            except Exception as ex:
                logger.error(f"Error during auto radio scan: {ex}")
            finally:
                with ZigbeeCollector._active_scan_lock:
                    ZigbeeCollector._active_scan_running = False
                    ZigbeeCollector._active_scan_started_at = 0.0

        th = threading.Thread(target=_scan_worker, daemon=True)
        th.start()
        return True

    def run_collection(self) -> Dict[str, Any]:
        """Performs non-invasive audit, analyzes diffs, and saves to database"""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        
        logger.info("Starting Zigbee passive data collection via SSH...")
        devices_raw, states, friendly_names, blocklist, log_raw, ha_mqtt_devices = self.fetch_raw_data()
        logger.info(f"Retrieved {len(devices_raw)} devices, {len(states)} states, {len(friendly_names)} names, {len(ha_mqtt_devices)} HA MQTT devices, log length: {len(log_raw)}")

        # Sync blocklist into DB (clears unblocked devices if blocklist was modified/emptied)
        sync_blocklist(blocklist or [], friendly_names)


        # Fetch topology links from logs
        links, scan_iso_ts = self.fetch_network_links()
        logger.info(f"Retrieved {len(links)} topology links (latest networkmap in logs: {scan_iso_ts})")
        if scan_iso_ts:
            curr_scan_ts = get_last_radio_scan_timestamp()
            if not curr_scan_ts or scan_iso_ts > curr_scan_ts:
                set_last_radio_scan_timestamp(scan_iso_ts)

        # Determine online status and friendly name for coordinator
        offline_delta = timedelta(hours=self.config["offline_threshold_hours"])
        online_status = {}
        for dev in devices_raw:
            ieee = dev.get("ieeeAddr")
            if not ieee:
                continue
            dev_type = dev.get("type", "Unknown")
            state = states.get(ieee, {})
            last_seen_str = state.get("last_seen", "")
            if not last_seen_str and dev.get("lastSeen"):
                try:
                    ts_epoch = dev.get("lastSeen")
                    if isinstance(ts_epoch, (int, float)):
                        last_seen_str = datetime.fromtimestamp(ts_epoch / 1000.0, tz=timezone.utc).isoformat()
                except Exception:
                    pass

            last_seen_dt = parse_iso_time(last_seen_str)
            is_recent = (now - last_seen_dt) <= offline_delta if last_seen_str else False
            if dev_type == "Coordinator":
                online_status[ieee] = True
                if ieee not in friendly_names:
                    friendly_names[ieee] = "Koordinátor (SLZB-06M LAN)"
            else:
                online_status[ieee] = is_recent

        conn = get_connection()
        cursor = conn.cursor()

        # Load existing known devices from DB (including known parent relationships)
        cursor.execute("SELECT ieee_addr, friendly_name, is_online, linkquality, battery, is_ignored, ignore_reason, parent_ieee, parent_name FROM devices")
        existing_devices = {row["ieee_addr"]: dict(row) for row in cursor.fetchall()}
        existing_parents = {ieee: d["parent_ieee"] for ieee, d in existing_devices.items() if d.get("parent_ieee")}

        # Compute full spanning tree topology parents (persisting last known parent on scan timeouts)
        parents = self.compute_network_topology_parents(devices_raw, links, online_status, existing_parents=existing_parents)

        total_devices = 0
        router_count = 0
        end_device_count = 0
        online_count = 0
        offline_count = 0
        weak_lqi_count = 0
        low_battery_count = 0

        current_ieees = {d.get("ieeeAddr") for d in devices_raw if d.get("ieeeAddr")}
        events_to_insert = []
        history_to_insert = []
        devices_to_upsert = []

        # Process filtered log lines for anomalies (ZDO leave, kicks, unknown entity errors, critical Z2M errors)
        left_network_devices = {}
        seen_batch_events = set()
        announced_devices = []
        if log_raw:
            for log_line in log_raw.split("\n"):
                log_line = log_line.strip()
                if not log_line:
                    continue

                # 1. ZDO Leave / Left the network
                m_left = re.search(r"warning:\s+z2m:\s+Device '([^']+)' left the network", log_line)
                m_leave_json = re.search(r'"type":\s*"device_leave".*?"friendly_name":\s*"([^"]+)"', log_line)
                dev_name = m_left.group(1) if m_left else (m_leave_json.group(1) if m_leave_json else None)

                if dev_name:
                    dev_ieee = next((i for i, n in friendly_names.items() if n == dev_name), None)
                    m_ts = re.search(r"^\[([0-9-]+\s+[0-9:]+)\]", log_line)
                    time_info = f" ({m_ts.group(1)})" if m_ts else ""
                    leave_msg = f"Odeslala rádiový signál ZDO Leave koordinátoru{time_info} – nutno fyzicky spárovat v terénu"
                    left_network_devices[dev_name] = leave_msg
                    if dev_ieee:
                        left_network_devices[dev_ieee] = leave_msg

                    if ("DEVICE_LEFT_NETWORK", dev_name) not in seen_batch_events:
                        seen_batch_events.add(("DEVICE_LEFT_NETWORK", dev_name))
                        cursor.execute("""
                            SELECT id FROM audit_events 
                            WHERE friendly_name = ? AND event_type = 'DEVICE_LEFT_NETWORK' AND timestamp >= ?
                        """, (dev_name, (now - timedelta(hours=24)).isoformat()))
                        if not cursor.fetchone():
                            events_to_insert.append((
                                now_iso,
                                "DEVICE_LEFT_NETWORK",
                                dev_ieee,
                                dev_name,
                                "danger",
                                f"Zařízení '{dev_name}' odeslalo rádiový signál ZDO Leave{time_info} a samovolně opustilo Zigbee síť!"
                            ))

                # 2. Coordinator kicked / ID conflict
                if "kicked from the network" in log_line or "ID conflict" in log_line:
                    m_addr = re.search(r"network address '([^']+)'", log_line) or re.search(r"for \"([^\"]+)\"", log_line)
                    conflict_addr = m_addr.group(1) if m_addr else ""
                    addr_desc = f" '{conflict_addr}'" if conflict_addr else ""
                    if ("DEVICE_KICKED", conflict_addr or "coordinator") not in seen_batch_events:
                        seen_batch_events.add(("DEVICE_KICKED", conflict_addr or "coordinator"))
                        cursor.execute("""
                            SELECT id FROM audit_events 
                            WHERE event_type = 'DEVICE_KICKED' AND timestamp >= ?
                        """, ((now - timedelta(hours=12)).isoformat(),))
                        if not cursor.fetchone():
                            events_to_insert.append((
                                now_iso,
                                "DEVICE_KICKED",
                                None,
                                "Koordinátor (EmberZNet)",
                                "warning",
                                f"Koordinátor detekoval kolizi adres{addr_desc} a zahájil samoopravu Zigbee PRO vykopnutím kolizní adresy ze sítě ({log_line[:120]})"
                            ))

                # Track device announcements / rejoining
                m_ann = re.search(r'"type":\s*"device_announce".*?"friendly_name":\s*"([^"]+)"', log_line)
                if m_ann:
                    dev_ann_name = m_ann.group(1)
                    if dev_ann_name not in announced_devices:
                        announced_devices.append(dev_ann_name)
                m_join = re.search(r"Accepting joining not in blocklist device '([^']+)'", log_line)
                if m_join:
                    j_ieee = m_join.group(1)
                    j_name = friendly_names.get(j_ieee, j_ieee)
                    if j_name not in announced_devices:
                        announced_devices.append(j_name)

                # 3. Unknown entity errors from HA
                m_unk = re.search(r"error:\s+z2m:\s+Entity '([^']+)' is unknown", log_line)
                if m_unk:
                    dev_name = m_unk.group(1)
                    dev_ieee = next((i for i, n in friendly_names.items() if n == dev_name), None)
                    if ("UNKNOWN_ENTITY_CALL", dev_name) not in seen_batch_events:
                        seen_batch_events.add(("UNKNOWN_ENTITY_CALL", dev_name))
                        cursor.execute("""
                            SELECT id FROM audit_events 
                            WHERE friendly_name = ? AND event_type = 'UNKNOWN_ENTITY_CALL' AND timestamp >= ?
                        """, (dev_name, (now - timedelta(hours=2)).isoformat()))
                        if not cursor.fetchone():
                            events_to_insert.append((
                                now_iso,
                                "UNKNOWN_ENTITY_CALL",
                                dev_ieee,
                                dev_name,
                                "danger",
                                f"Home Assistant volá neexistující entitu '{dev_name}' (chyba Entity is unknown). Zkontrolujte zóny TRV / MQTT v HA."
                            ))

                # 4. Other unexpected Z2M errors (excluding drawer sockets LQI)
                if " error: " in log_line and "is unknown" not in log_line and "Failed to execute LQI for 'Zasuvka_" not in log_line:
                    err_clean = re.sub(r"^\[[^\]]+\]\s*", "", log_line).strip()[:140]
                    if ("Z2M_ERROR", err_clean[:40]) not in seen_batch_events:
                        seen_batch_events.add(("Z2M_ERROR", err_clean[:40]))
                        cursor.execute("""
                            SELECT id FROM audit_events 
                            WHERE event_type = 'Z2M_ERROR' AND message LIKE ? AND timestamp >= ?
                        """, (f"%{err_clean[:60]}%", (now - timedelta(hours=2)).isoformat()))
                        if not cursor.fetchone():
                            events_to_insert.append((
                                now_iso,
                                "Z2M_ERROR",
                                None,
                                "Zigbee2MQTT",
                                "warning",
                                f"Chyba v logu Z2M: {err_clean}"
                            ))

        # 2b. Auto-resolution check for ID conflicts
        has_current_kick = any(e[1] == "DEVICE_KICKED" for e in events_to_insert)
        if not has_current_kick:
            cursor.execute("""
                SELECT id FROM audit_events 
                WHERE event_type = 'DEVICE_KICKED' AND timestamp >= ?
            """, ((now - timedelta(hours=12)).isoformat(),))
            has_kick = cursor.fetchone() is not None
        else:
            has_kick = True

        if has_kick:
            cursor.execute("""
                SELECT id FROM audit_events 
                WHERE event_type = 'CONFLICT_RESOLVED' AND timestamp >= ?
            """, ((now - timedelta(hours=12)).isoformat(),))
            has_resolved = cursor.fetchone() is not None or any(e[1] == "CONFLICT_RESOLVED" for e in events_to_insert)
            if not has_resolved:
                devs_info = f" (zařízení {', '.join(announced_devices[:3])})" if announced_devices else ""
                events_to_insert.append((
                    now_iso,
                    "CONFLICT_RESOLVED",
                    None,
                    "Koordinátor (EmberZNet)",
                    "success",
                    f"Samooprava úspěšná: kolize adres byla koordinátorem vyřešena{devs_info}. Zařízení se automaticky znovu ohlásila s novými adresami a jsou ONLINE."
                ))

        # 3-Way Cross-Check:
        # A) HA Orphans (CRITICAL): Registered in Home Assistant MQTT device registry, but NOT in Zigbee radio (database.db)
        ha_orphans = {}
        for h_ieee, h_fn in ha_mqtt_devices.items():
            if h_ieee not in current_ieees:
                ha_orphans[h_ieee] = h_fn

        # B) Z2M Reserved (INFORMATIONAL): Defined in configuration.yaml, but NOT in database.db AND NOT in HA MQTT
        z2m_reserved = {}
        for g_ieee, g_fn in friendly_names.items():
            if g_ieee not in current_ieees and g_ieee not in blocklist:
                if not g_fn.startswith("Zasuvka_"):  # drawer sockets are reserve, not ghosts
                    if g_ieee not in ha_orphans:
                        z2m_reserved[g_ieee] = g_fn

        # Sync 3-way status into orphaned_devices table
        sync_orphaned_devices(
            active_ieees=current_ieees,
            ha_orphans=ha_orphans,
            z2m_reserved=z2m_reserved,
            left_network_names=left_network_devices
        )

        # Audit events: Only raise CRITICAL alarm for HA orphans (phantom devices in Home Assistant causing errors)
        for h_ieee, h_fn in ha_orphans.items():
            cursor.execute("""
                SELECT id FROM audit_events 
                WHERE ieee_addr = ? AND event_type = 'ORPHAN_HA_DEVICE' AND timestamp >= ?
            """, (h_ieee, (now - timedelta(hours=12)).isoformat()))
            if not cursor.fetchone():
                events_to_insert.append((
                    now_iso,
                    "ORPHAN_HA_DEVICE",
                    h_ieee,
                    h_fn,
                    "danger",
                    f"Home Assistant v MQTT eviduje zařízení '{h_fn}' ({h_ieee}), které však fyzicky neexistuje v síti Zigbee! Hrozí cyklické chyby a falešná regulace."
                ))

        removed_ieees = [old_ieee for old_ieee in existing_devices if old_ieee not in current_ieees]
        # Process removed devices FIRST
        removed_devices_by_name = {}
        for old_ieee in removed_ieees:
            old_info = existing_devices[old_ieee]
            old_fn = old_info["friendly_name"]
            removed_devices_by_name[old_fn] = old_ieee
            events_to_insert.append((
                now_iso,
                "DEVICE_REMOVED",
                old_ieee,
                old_fn,
                "danger",
                f"Zařízení odebráno ze sítě: {old_fn} ({old_ieee})"
            ))

        # Check existing devices against blocklist
        for dev in devices_raw:
            d_ieee = dev.get("ieeeAddr")
            if d_ieee and d_ieee in blocklist:
                d_fn = friendly_names.get(d_ieee, d_ieee)
                cursor.execute("""
                    SELECT id FROM audit_events 
                    WHERE ieee_addr = ? AND event_type = 'BLOCKED_DEVICE_DETECTED' AND timestamp >= ?
                """, (d_ieee, (now - timedelta(hours=12)).isoformat()))
                if not cursor.fetchone():
                    events_to_insert.append((
                        now_iso,
                        "BLOCKED_DEVICE_DETECTED",
                        d_ieee,
                        d_fn,
                        "danger",
                        f"POZOR: Zařízení {d_fn} ({d_ieee}) je na BLOCKLISTU a je připojeno v síti!"
                    ))

        offline_delta = timedelta(hours=self.config["offline_threshold_hours"])

        for dev in devices_raw:
            ieee = dev.get("ieeeAddr")
            if not ieee:
                continue
            total_devices += 1
            dev_type = dev.get("type", "Unknown")
            if dev_type == "Router":
                router_count += 1
            elif dev_type == "EndDevice":
                end_device_count += 1

            friendly_name = friendly_names.get(ieee, dev.get("friendly_name", ieee))
            model_id = dev.get("modelId", "")
            manuf_name = dev.get("manufName", "")
            power_source = dev.get("powerSource", "")

            state = states.get(ieee, {})
            lqi = state.get("linkquality")
            battery = state.get("battery")
            
            # Compare state.json and database.db lastSeen to always take the most recent live timestamp
            last_seen_str = state.get("last_seen", "")
            last_seen_dt = parse_iso_time(last_seen_str) if last_seen_str else datetime.min.replace(tzinfo=timezone.utc)
            
            if dev.get("lastSeen"):
                try:
                    ts_epoch = dev.get("lastSeen")
                    if isinstance(ts_epoch, (int, float)):
                        db_last_seen_dt = datetime.fromtimestamp(ts_epoch / 1000.0, tz=timezone.utc)
                        if db_last_seen_dt > last_seen_dt:
                            last_seen_dt = db_last_seen_dt
                            last_seen_str = db_last_seen_dt.isoformat()
                except Exception:
                    pass

            is_recent = (now - last_seen_dt) <= offline_delta if last_seen_str else False
            
            # Coordinator is always considered online
            if dev_type == "Coordinator":
                is_online = 1
            else:
                is_online = 1 if is_recent else 0

            if is_online:
                online_count += 1
            else:
                offline_count += 1

            # Check weak LQI
            if is_online and lqi is not None and lqi < self.config["lqi_weak_threshold"] and dev_type != "Coordinator":
                weak_lqi_count += 1

            # Check low battery
            if battery is not None and battery <= self.config["battery_low_threshold"]:
                low_battery_count += 1

            # Event detection: New device / Replacement
            if ieee not in existing_devices:
                # 1. Blocklist warning if newly joined
                if ieee in blocklist:
                    events_to_insert.append((
                        now_iso,
                        "BLOCKED_DEVICE_DETECTED",
                        ieee,
                        friendly_name,
                        "danger",
                        f"POZOR: Zařízení {friendly_name} ({ieee}) je na BLOCKLISTU a pokusilo se připojit k síti!"
                    ))

                # 2. 1-to-1 replacement detection by friendly_name
                old_replaced_ieee = None
                if friendly_name in removed_devices_by_name:
                    old_replaced_ieee = removed_devices_by_name[friendly_name]
                else:
                    cursor.execute("""
                        SELECT ieee_addr FROM audit_events 
                        WHERE friendly_name = ? AND event_type = 'DEVICE_REMOVED' AND ieee_addr != ?
                        ORDER BY id DESC LIMIT 1
                    """, (friendly_name, ieee))
                    row_rem = cursor.fetchone()
                    if row_rem:
                        old_replaced_ieee = row_rem["ieee_addr"]

                if old_replaced_ieee and old_replaced_ieee != ieee:
                    record_device_replacement(
                        friendly_name=friendly_name,
                        old_ieee=old_replaced_ieee,
                        new_ieee=ieee,
                        reason="Výměna kus za kus (shodný název)",
                        details=f"Původní HW ({old_replaced_ieee}) nahrazen novým kusem ({ieee})."
                    )
                    rep_msg = f"Výměna kus za kus: '{friendly_name}' (původní: {old_replaced_ieee} ➔ nový: {ieee})."
                    events_to_insert.append((
                        now_iso,
                        "DEVICE_REPLACED",
                        ieee,
                        friendly_name,
                        "warning",
                        rep_msg
                    ))

                add_msg = f"Nové zařízení detekováno: {friendly_name} ({dev_type}, {model_id})"
                events_to_insert.append((
                    now_iso,
                    "DEVICE_ADDED",
                    ieee,
                    friendly_name,
                    "info",
                    add_msg
                ))
            else:
                prev = existing_devices[ieee]
                # Online / Offline state change
                if prev["is_online"] != is_online:
                    if is_online == 0:
                        events_to_insert.append((
                            now_iso,
                            "DEVICE_OFFLINE",
                            ieee,
                            friendly_name,
                            "warning",
                            f"Zařízení přestalo odpovídat (poslední aktivita: {last_seen_str or 'neznámá'})"
                        ))
                    else:
                        events_to_insert.append((
                            now_iso,
                            "DEVICE_ONLINE",
                            ieee,
                            friendly_name,
                            "info",
                            f"Zařízení je opět online (LQI: {lqi})"
                        ))

                # Low battery transition
                prev_batt = prev.get("battery")
                if battery is not None and battery <= self.config["battery_low_threshold"]:
                    if prev_batt is None or prev_batt > self.config["battery_low_threshold"]:
                        events_to_insert.append((
                            now_iso,
                            "LOW_BATTERY",
                            ieee,
                            friendly_name,
                            "danger",
                            f"Kriticky nízká baterie: {battery}%"
                        ))

            raw_state_json = json.dumps(state)
            first_seen = existing_devices.get(ieee, {}).get("first_seen", now_iso)
            parent_ieee = parents.get(ieee)
            if not parent_ieee and is_online:
                parent_ieee = existing_devices.get(ieee, {}).get("parent_ieee")
            parent_name = friendly_names.get(parent_ieee, parent_ieee) if parent_ieee else None

            # Ignore / Mute status handling and Auto-wakeup
            prev_info = existing_devices.get(ieee)
            prev_ignored = prev_info.get("is_ignored", 0) if prev_info else 0
            prev_reason = prev_info.get("ignore_reason") if prev_info else None

            is_ignored = prev_ignored
            ignore_reason = prev_reason

            if is_online == 1 and prev_ignored == 1:
                # Device woke up! Automatically clear ignored state
                is_ignored = 0
                ignore_reason = None
                events_to_insert.append((
                    now_iso,
                    "IGNORED_DEVICE_BACK_ONLINE",
                    ieee,
                    friendly_name,
                    "info",
                    f"Ztišené zařízení se probudilo a odeslalo data. Bylo automaticky odtišeno.",
                    None,
                    None
                ))

            devices_to_upsert.append((
                ieee,
                friendly_name,
                dev_type,
                model_id,
                manuf_name,
                power_source,
                lqi,
                battery,
                last_seen_str,
                is_online,
                first_seen,
                now_iso,
                raw_state_json,
                parent_ieee,
                parent_name,
                is_ignored,
                ignore_reason
            ))

            history_to_insert.append((
                ieee,
                now_iso,
                lqi,
                battery,
                is_online
            ))


        # Count ignored offline devices
        ignored_count = sum(1 for d in devices_to_upsert if d[15] == 1 and d[9] == 0)

        # Save snapshot
        cursor.execute("""
            INSERT INTO snapshots (
                timestamp, total_devices, routers, end_devices,
                online_count, offline_count, weak_lqi_count, low_battery_count, ignored_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now_iso, total_devices, router_count, end_device_count, online_count, offline_count, weak_lqi_count, low_battery_count, ignored_count))
        snapshot_id = cursor.lastrowid

        # Upsert devices
        cursor.executemany("""
            INSERT INTO devices (
                ieee_addr, friendly_name, device_type, model_id, manuf_name,
                power_source, linkquality, battery, last_seen, is_online,
                first_seen, updated_at, raw_state, parent_ieee, parent_name,
                is_ignored, ignore_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ieee_addr) DO UPDATE SET
                friendly_name = excluded.friendly_name,
                device_type = excluded.device_type,
                model_id = excluded.model_id,
                manuf_name = excluded.manuf_name,
                power_source = excluded.power_source,
                linkquality = excluded.linkquality,
                battery = excluded.battery,
                last_seen = excluded.last_seen,
                is_online = excluded.is_online,
                updated_at = excluded.updated_at,
                raw_state = excluded.raw_state,
                parent_ieee = COALESCE(excluded.parent_ieee, devices.parent_ieee),
                parent_name = COALESCE(excluded.parent_name, devices.parent_name),
                is_ignored = excluded.is_ignored,
                ignore_reason = excluded.ignore_reason
        """, devices_to_upsert)

        # Clean removed devices from active devices table to prevent recurring false alarms
        if removed_ieees:
            cursor.executemany("DELETE FROM devices WHERE ieee_addr = ?", [(x,) for x in removed_ieees])
            cursor.executemany("DELETE FROM network_links WHERE source_ieee = ? OR target_ieee = ?", [(x, x) for x in removed_ieees])

        # Upsert network links
        if links:
            links_to_insert = []
            for l in links:
                s = l.get("sourceIeeeAddr")
                t = l.get("targetIeeeAddr")
                lqi = l.get("lqi", l.get("linkquality"))
                rel = l.get("relationship")
                if s and t:
                    links_to_insert.append((s, t, lqi, rel, now_iso))
            cursor.executemany("""
                INSERT INTO network_links (source_ieee, target_ieee, lqi, relationship, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_ieee, target_ieee) DO UPDATE SET
                    lqi = excluded.lqi,
                    relationship = excluded.relationship,
                    updated_at = excluded.updated_at
            """, links_to_insert)

        # Insert history (keep clean)
        cursor.executemany("""
            INSERT INTO device_history (ieee_addr, timestamp, linkquality, battery, is_online)
            VALUES (?, ?, ?, ?, ?)
        """, history_to_insert)

        # Insert events
        if events_to_insert:
            cursor.executemany("""
                INSERT INTO audit_events (timestamp, event_type, ieee_addr, friendly_name, severity, message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, events_to_insert)

        conn.commit()
        conn.close()

        # Enforce automated data retention (purges data older than configured retention_days)
        try:
            purge_res = purge_old_history(retention_days=self.config.get("retention_days", 120))
            if any(purge_res.values()):
                logger.info(f"Data retention policy applied: {purge_res}")
        except Exception as e:
            logger.warning(f"Error applying data retention policy: {e}")

        # Check if new devices were added to the network (excluding initial import baseline)
        new_device_names = [e[3] for e in events_to_insert if e[1] == "DEVICE_ADDED"]
        if new_device_names and len(existing_devices) > 0:
            logger.info(f"Detected {len(new_device_names)} newly added device(s). Evaluating auto radio scan trigger...")
            self.trigger_auto_radio_scan_if_needed(new_device_names)

        summary = {
            "snapshot_id": snapshot_id,
            "timestamp": now_iso,
            "total_devices": total_devices,
            "routers": router_count,
            "end_devices": end_device_count,
            "online": online_count,
            "offline": offline_count,
            "ignored_offline": ignored_count,
            "actionable_offline": max(0, offline_count - ignored_count),
            "weak_lqi": weak_lqi_count,
            "low_battery": low_battery_count,
            "new_events_count": len(events_to_insert),
            "new_devices_count": len(new_device_names)
        }
        logger.info(f"Audit completed successfully: {summary}")
        return summary

if __name__ == "__main__":
    collector = ZigbeeCollector()
    res = collector.run_collection()
    print(json.dumps(res, indent=2))
