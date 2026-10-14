import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import os
from pathlib import Path

ENV_DB_PATH = os.getenv("DB_PATH")
if ENV_DB_PATH:
    DB_PATH = Path(ENV_DB_PATH)
elif Path("/data").is_dir() and os.access("/data", os.W_OK):
    DB_PATH = Path("/data/zigbee_monitor.db")
else:
    DB_PATH = Path(__file__).parent / "data" / "zigbee_monitor.db"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # Snapshots table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_devices INTEGER NOT NULL,
                routers INTEGER NOT NULL,
                end_devices INTEGER NOT NULL,
                online_count INTEGER NOT NULL,
                offline_count INTEGER NOT NULL,
                weak_lqi_count INTEGER NOT NULL,
                low_battery_count INTEGER NOT NULL
            )
        """)
        
        # Current device state snapshot
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                ieee_addr TEXT PRIMARY KEY,
                friendly_name TEXT,
                device_type TEXT,
                model_id TEXT,
                manuf_name TEXT,
                power_source TEXT,
                linkquality INTEGER,
                battery REAL,
                last_seen TEXT,
                is_online INTEGER,
                first_seen TEXT,
                updated_at TEXT,
                raw_state TEXT,
                parent_ieee TEXT,
                parent_name TEXT
            )
        """)

        # Network topology links table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS network_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_ieee TEXT NOT NULL,
                target_ieee TEXT NOT NULL,
                lqi INTEGER,
                relationship INTEGER,
                updated_at TEXT,
                UNIQUE(source_ieee, target_ieee)
            )
        """)
        
        # Ensure columns exist if table was already created
        cursor.execute("PRAGMA table_info(devices)")
        existing_cols = [row[1] for row in cursor.fetchall()]
        if "parent_ieee" not in existing_cols:
            cursor.execute("ALTER TABLE devices ADD COLUMN parent_ieee TEXT")
        if "parent_name" not in existing_cols:
            cursor.execute("ALTER TABLE devices ADD COLUMN parent_name TEXT")
        if "is_ignored" not in existing_cols:
            cursor.execute("ALTER TABLE devices ADD COLUMN is_ignored INTEGER DEFAULT 0")
        if "ignore_reason" not in existing_cols:
            cursor.execute("ALTER TABLE devices ADD COLUMN ignore_reason TEXT")
            # Auto-tag known drawer sockets as ignored on initial migration
            cursor.execute("""
                UPDATE devices 
                SET is_ignored = 1, ignore_reason = 'Záložní v šuplíku (rezerva)'
                WHERE friendly_name LIKE 'Zasuvka_%' AND is_online = 0
            """)

        cursor.execute("PRAGMA table_info(snapshots)")
        snap_cols = [row[1] for row in cursor.fetchall()]
        if "ignored_count" not in snap_cols:
            cursor.execute("ALTER TABLE snapshots ADD COLUMN ignored_count INTEGER DEFAULT 0")

        # History table for tracking metrics over time
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ieee_addr TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                linkquality INTEGER,
                battery REAL,
                is_online INTEGER
            )
        """)
        
        # Audit log / Events (Additions, Removals, Drops, Alerts)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                ieee_addr TEXT,
                friendly_name TEXT,
                severity TEXT NOT NULL,
                message TEXT NOT NULL
            )
        """)

        # Hardware replacements table (History of 1-to-1 replacements per friendly_name)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device_replacements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                friendly_name TEXT NOT NULL,
                old_ieee TEXT NOT NULL,
                new_ieee TEXT NOT NULL,
                reason TEXT,
                details TEXT
            )
        """)

        # Hardware blocklist table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS hardware_blocklist (
                ieee_addr TEXT PRIMARY KEY,
                friendly_name TEXT,
                reason TEXT,
                detected_at TEXT NOT NULL
            )
        """)

        # Orphaned / ghost devices table (present in config or left network, missing from active DB)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orphaned_devices (
                ieee_addr TEXT PRIMARY KEY,
                friendly_name TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                detected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        """)

        # Clean up legacy author / client_info columns if present
        for tbl, col in [
            ("audit_events", "author"),
            ("audit_events", "client_info"),
            ("device_replacements", "author"),
            ("device_replacements", "client_info"),
            ("hardware_blocklist", "author"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE {tbl} DROP COLUMN {col}")
            except Exception:
                pass

        # Backfill historical details for the 208HL-P replacement on Oct 7
        cursor.execute("""
            UPDATE hardware_blocklist
            SET friendly_name = '208HL-P (stará hlavice)',
                reason = 'Vadný kus vyřazen při výměně za novou TRVZB (0x08ddebfffef22412)',
                detected_at = '2026-10-07T20:42:58'
            WHERE ieee_addr = '0xc09b9efffe4b47b9'
        """)

        # Also register into device_replacements if not already present
        cursor.execute("SELECT id FROM device_replacements WHERE old_ieee = '0xc09b9efffe4b47b9'")
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO device_replacements (
                    timestamp, friendly_name, old_ieee, new_ieee, reason, details
                ) VALUES (
                    '2026-10-07T20:42:58', '208HL-P', '0xc09b9efffe4b47b9', '0x08ddebfffef22412',
                    'Výměna kus za kus v Zrcadlové místnosti',
                    'Původní vadná TRVZB hlavice vyřazena do blocklistu, nová napárována.'
                )
            """)
        
        # Performance indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_device_history_ieee_ts ON device_history (ieee_addr, timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_device_history_ts ON device_history (timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots (timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_ts ON audit_events (timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_replacements_fn ON device_replacements (friendly_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_replacements_ts ON device_replacements (timestamp)")

        # Settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL
            )
        """)

        # Sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        # Seed or migrate default admin and technik (neutral technician) users
        cursor.execute("SELECT username FROM users")
        existing_users = {row[0] for row in cursor.fetchall()}
        import hashlib, secrets
        now_str = datetime.now().isoformat()
        if 'admin' not in existing_users:
            salt = secrets.token_hex(16)
            pwd_hash = hashlib.pbkdf2_hmac('sha256', 'admin'.encode('utf-8'), bytes.fromhex(salt), 100000).hex()
            cursor.execute("""
                INSERT INTO users (username, password_hash, salt, role, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, ('admin', pwd_hash, salt, 'admin', now_str))

        if 'jarek' in existing_users and 'technik' not in existing_users:
            cursor.execute("UPDATE users SET username = 'technik' WHERE username = 'jarek'")
            cursor.execute("UPDATE sessions SET username = 'technik' WHERE username = 'jarek'")
        elif 'technik' not in existing_users and 'jarek' not in existing_users:
            salt = secrets.token_hex(16)
            pwd_hash = hashlib.pbkdf2_hmac('sha256', 'technik'.encode('utf-8'), bytes.fromhex(salt), 100000).hex()
            cursor.execute("""
                INSERT INTO users (username, password_hash, salt, role, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, ('technik', pwd_hash, salt, 'technician', now_str))

        conn.commit()

import hashlib
import secrets

def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 100000).hex()
    return pwd_hash, salt

def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), 100000).hex()
    return secrets.compare_digest(pwd_hash, expected_hash)

def create_user_session(username: str, duration_hours: int = 72) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(hours=duration_hours)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions (token, username, created_at, expires_at)
        VALUES (?, ?, ?, ?)
    """, (token, username, now.isoformat(), expires.isoformat()))
    conn.commit()
    conn.close()
    return token

def get_session_user(token: str) -> Optional[dict]:
    if not token:
        return None
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT s.token, s.expires_at, u.username, u.role
        FROM sessions s
        JOIN users u ON s.username = u.username
        WHERE s.token = ?
    """, (token,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    expires_dt = datetime.fromisoformat(row["expires_at"])
    if datetime.now() > expires_dt:
        return None
    return dict(row)

def delete_user_session(token: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()

from datetime import timedelta
def get_connection():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def set_device_ignore_status(ieee_addr: str, is_ignored: bool, reason: Optional[str] = None) -> bool:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT friendly_name FROM devices WHERE ieee_addr = ?", (ieee_addr,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    
    friendly_name = row["friendly_name"] or ieee_addr
    now_iso = datetime.now().isoformat()
    val_ignored = 1 if is_ignored else 0
    val_reason = reason.strip() if (is_ignored and reason and reason.strip()) else ("Záložní v šuplíku (rezerva)" if is_ignored else None)

    c.execute("""
        UPDATE devices 
        SET is_ignored = ?, ignore_reason = ? 
        WHERE ieee_addr = ?
    """, (val_ignored, val_reason, ieee_addr))

    if is_ignored:
        c.execute("""
            INSERT INTO audit_events (timestamp, event_type, ieee_addr, friendly_name, severity, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now_iso, "DEVICE_MUTED", ieee_addr, friendly_name, "info", f"Zařízení bylo ztišeno (očekávaný offline: {val_reason})."))
    else:
        c.execute("""
            INSERT INTO audit_events (timestamp, event_type, ieee_addr, friendly_name, severity, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now_iso, "DEVICE_UNMUTED", ieee_addr, friendly_name, "info", "Ztišení zařízení bylo zrušeno. Zařízení je opět plně monitorováno."))

    conn.commit()
    conn.close()
    return True

def purge_old_history(retention_days: int = 120) -> Dict[str, int]:
    """Purge records older than retention_days to maintain optimal database size."""
    from datetime import timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    purged = {}
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM device_history WHERE timestamp < ?", (cutoff,))
        purged["history_deleted"] = cursor.rowcount
        cursor.execute("DELETE FROM snapshots WHERE timestamp < ?", (cutoff,))
        purged["snapshots_deleted"] = cursor.rowcount
        cursor.execute("DELETE FROM audit_events WHERE timestamp < ?", (cutoff,))
        purged["events_deleted"] = cursor.rowcount
        conn.commit()
    return purged


def record_device_replacement(
    friendly_name: str,
    old_ieee: str,
    new_ieee: str,
    reason: Optional[str] = "Výměna kus za kus",
    details: Optional[str] = None
) -> int:
    """Records a 1-to-1 hardware replacement event into device_replacements table."""
    now_iso = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO device_replacements (
                timestamp, friendly_name, old_ieee, new_ieee, reason, details
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (now_iso, friendly_name, old_ieee, new_ieee, reason, details))
        conn.commit()
        return cursor.lastrowid


def get_device_replacements(friendly_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves list of all hardware replacements, optionally filtered by friendly_name."""
    conn = get_connection()
    cursor = conn.cursor()
    if friendly_name:
        cursor.execute("""
            SELECT * FROM device_replacements 
            WHERE friendly_name = ?
            ORDER BY id DESC
        """, (friendly_name,))
    else:
        cursor.execute("SELECT * FROM device_replacements ORDER BY id DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def sync_blocklist(blocklist_ieees: List[str], friendly_names_map: Optional[Dict[str, str]] = None):
    """Synchronizes blocklist from configuration.yaml into hardware_blocklist table."""
    now_iso = datetime.now().isoformat()
    names = friendly_names_map or {}
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for ieee in blocklist_ieees:
            fname = names.get(ieee, ieee)
            cursor.execute("""
                INSERT INTO hardware_blocklist (ieee_addr, friendly_name, reason, detected_at)
                VALUES (?, ?, 'Zablokováno v configuration.yaml Z2M', ?)
                ON CONFLICT(ieee_addr) DO UPDATE SET
                    friendly_name = COALESCE(excluded.friendly_name, hardware_blocklist.friendly_name)
            """, (ieee, fname, now_iso))
        conn.commit()


def get_blocklist() -> List[Dict[str, Any]]:
    """Returns all blocked hardware records."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM hardware_blocklist ORDER BY detected_at DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def sync_orphaned_devices(
    active_ieees: set,
    ha_orphans: Dict[str, str],
    z2m_reserved: Dict[str, str],
    left_network_names: Optional[Dict[str, str]] = None
):
    """
    3-way synchronization of orphaned/reserved devices:
    - active_ieees: IEEE addresses active in Zigbee database.db
    - ha_orphans: {ieee: name} devices registered in Home Assistant MQTT but missing from Zigbee radio (CRITICAL)
    - z2m_reserved: {ieee: name} devices defined in Z2M configuration.yaml, not in radio, not in HA (WAITING FOR PAIR)
    - left_network_names: {name/ieee: details} for devices that sent ZDO leave
    """
    now_iso = datetime.now().isoformat()
    left_names = left_network_names or {}
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # 1. Any device now active in database.db is no longer orphaned
        if active_ieees:
            placeholders = ",".join("?" for _ in active_ieees)
            cursor.execute(f"""
                UPDATE orphaned_devices
                SET is_active = 0, updated_at = ?
                WHERE is_active = 1 AND ieee_addr IN ({placeholders})
            """, [now_iso] + list(active_ieees))

        # 2. Clear devices that were in ha_orphans or z2m_reserved previously but are neither anymore
        all_reported = set(ha_orphans.keys()) | set(z2m_reserved.keys())
        if all_reported:
            placeholders = ",".join("?" for _ in all_reported)
            cursor.execute(f"""
                UPDATE orphaned_devices
                SET is_active = 0, updated_at = ?
                WHERE is_active = 1 AND ieee_addr NOT IN ({placeholders})
            """, [now_iso] + list(all_reported))
        else:
            cursor.execute("""
                UPDATE orphaned_devices
                SET is_active = 0, updated_at = ?
                WHERE is_active = 1
            """, (now_iso,))

        # Fetch existing active devices to preserve status (especially LEFT_NETWORK)
        cursor.execute("SELECT ieee_addr, friendly_name, status, details, detected_at FROM orphaned_devices WHERE is_active = 1")
        existing_status_map = {row[0]: {"friendly_name": row[1], "status": row[2], "details": row[3], "detected_at": row[4]} for row in cursor.fetchall()}

        # 3. Upsert HA orphans (CRITICAL)
        for ieee, fname in ha_orphans.items():
            existing = existing_status_map.get(ieee, {})
            details = left_names.get(fname, left_names.get(ieee, existing.get("details", "V Home Assistantu (MQTT), ale chybí v rádiu Zigbee!")))
            cursor.execute("""
                INSERT INTO orphaned_devices (ieee_addr, friendly_name, status, details, detected_at, updated_at, is_active)
                VALUES (?, ?, 'ORPHAN_HA', ?, ?, ?, 1)
                ON CONFLICT(ieee_addr) DO UPDATE SET
                    friendly_name = excluded.friendly_name,
                    status = excluded.status,
                    details = excluded.details,
                    updated_at = excluded.updated_at,
                    is_active = 1
            """, (ieee, fname, details, now_iso, now_iso))

        # 4. Upsert Z2M reserved (and PRESERVE LEFT_NETWORK!)
        for ieee, fname in z2m_reserved.items():
            existing = existing_status_map.get(ieee, {})
            was_left_network = (existing.get("status") == "LEFT_NETWORK")
            is_new_left_network = (fname in left_names or ieee in left_names)

            if is_new_left_network:
                status = "LEFT_NETWORK"
                details = left_names.get(fname, left_names.get(ieee, "Odeslala rádiový signál ZDO Leave koordinátoru (odpárováno)"))
                detected_at = now_iso
            elif was_left_network:
                status = "LEFT_NETWORK"
                details = existing.get("details", "Odeslala rádiový signál ZDO Leave koordinátoru")
                detected_at = existing.get("detected_at", now_iso)
            else:
                status = "Z2M_RESERVED"
                details = "V konfiguraci Z2M, není v HA ani v síti (čeká na spárování)"
                detected_at = existing.get("detected_at", now_iso)

            cursor.execute("""
                INSERT INTO orphaned_devices (ieee_addr, friendly_name, status, details, detected_at, updated_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(ieee_addr) DO UPDATE SET
                    friendly_name = excluded.friendly_name,
                    status = excluded.status,
                    details = excluded.details,
                    updated_at = excluded.updated_at,
                    is_active = 1
            """, (ieee, fname, status, details, detected_at, now_iso))

        conn.commit()


def get_orphaned_devices(only_active: bool = True) -> List[Dict[str, Any]]:
    """Returns orphaned devices."""
    conn = get_connection()
    cursor = conn.cursor()
    if only_active:
        cursor.execute("SELECT * FROM orphaned_devices WHERE is_active = 1 ORDER BY updated_at DESC")
    else:
        cursor.execute("SELECT * FROM orphaned_devices ORDER BY updated_at DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_network_anomalies(hours: int = 48) -> Dict[str, Any]:
    """Retrieves 3-way anomalies (HA orphans, Z2M reserved, left network, unknown entity calls)."""
    from datetime import timezone, timedelta
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(hours=hours)).isoformat()

    conn = get_connection()
    c = conn.cursor()

    # Left network or kicked from audit_events (within window)
    c.execute("""
        SELECT timestamp, event_type, ieee_addr, friendly_name, severity, message
        FROM audit_events
        WHERE event_type IN ('DEVICE_LEFT_NETWORK', 'DEVICE_KICKED') AND timestamp >= ?
        ORDER BY id DESC
    """, (cutoff,))
    left_net_events = [dict(r) for r in c.fetchall()]

    # Unknown entity calls from HA (only show active/recent within last 1 hour; resolved errors drop from banner)
    cutoff_active_calls = (now_dt - timedelta(hours=1)).isoformat()
    c.execute("""
        SELECT timestamp, event_type, ieee_addr, friendly_name, severity, message
        FROM audit_events
        WHERE event_type = 'UNKNOWN_ENTITY_CALL' AND timestamp >= ?
        ORDER BY id DESC
    """, (cutoff_active_calls,))
    unknown_calls = [dict(r) for r in c.fetchall()]

    # Active orphaned devices categorized
    c.execute("""
        SELECT ieee_addr, friendly_name, status, details, detected_at, updated_at
        FROM orphaned_devices
        WHERE is_active = 1
        ORDER BY updated_at DESC
    """)
    all_orphans = [dict(r) for r in c.fetchall()]
    conn.close()

    ha_orphans = [d for d in all_orphans if d["status"] == "ORPHAN_HA"]
    z2m_reserved = [d for d in all_orphans if d["status"] in ("Z2M_RESERVED", "CONFIG_ONLY")]
    left_network_devices = [d for d in all_orphans if d["status"] == "LEFT_NETWORK"]

    has_critical = bool(ha_orphans or unknown_calls or left_network_devices)
    has_anomalies = bool(ha_orphans or unknown_calls or left_net_events or left_network_devices or z2m_reserved)

    return {
        "left_network": left_net_events,
        "left_network_devices": left_network_devices,
        "unknown_entity_calls": unknown_calls,
        "ha_orphans": ha_orphans,
        "z2m_reserved": z2m_reserved,
        "orphaned_devices": all_orphans,
        "has_critical": has_critical,
        "has_anomalies": has_anomalies,
        "count": len(ha_orphans) + len(unknown_calls) + len(left_network_devices) + len(z2m_reserved)
    }


def get_last_radio_scan_timestamp() -> Optional[str]:
    """Returns ISO timestamp of the last active radio scan."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'last_radio_scan'")
    row = c.fetchone()
    conn.close()
    if row and row["value"]:
        return row["value"]
    return None


def set_last_radio_scan_timestamp(ts_iso: str):
    """Saves timestamp of the completed active radio scan."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO settings (key, value) VALUES ('last_radio_scan', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (ts_iso,))
    conn.commit()
    conn.close()




