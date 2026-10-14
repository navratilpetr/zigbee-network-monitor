import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

import sqlite3
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request, Response, Depends, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import (
    get_connection, init_db, hash_password, verify_password,
    create_user_session, get_session_user, delete_user_session,
    set_device_ignore_status, get_device_replacements, get_blocklist,
    get_network_anomalies, get_orphaned_devices,
    get_last_radio_scan_timestamp, set_last_radio_scan_timestamp
)
from collector import ZigbeeCollector
from ai_service import get_condensed_summary, generate_technician_checklist, ask_ai_consultant

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Zigbee Monitor", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

collector = ZigbeeCollector()
is_scanning = False

class LoginRequest(BaseModel):
    username: str
    password: str

class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "admin"

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class QuestionRequest(BaseModel):
    question: str
    api_key: Optional[str] = None

class SettingsRequest(BaseModel):
    interval_minutes: Optional[int] = 5
    gemini_api_key: Optional[str] = None

class DeviceIgnoreRequest(BaseModel):
    is_ignored: bool
    reason: Optional[str] = None

def get_current_user(request: Request) -> Optional[dict]:
    # Seamless single-sign-on when running inside Home Assistant Ingress
    if request.headers.get("X-Ingress-Path") or request.headers.get("X-Hassio-Key") or os.getenv("SUPERVISOR_TOKEN"):
        return {"id": 1, "username": "ha_admin", "role": "admin", "is_ingress": True}
    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    user = get_session_user(token)
    if user:
        user["is_ingress"] = False
    return user


def require_user(user: Optional[dict] = Depends(get_current_user)) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="Nejste přihlášen(a)")
    return user

async def periodic_audit_task():
    """Background task running periodic passive audits."""
    global is_scanning
    logger.info("Periodic audit task started.")
    while True:
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'interval_minutes'")
            row = c.fetchone()
            interval = int(row["value"]) if row and row["value"] else 5
            conn.close()

            logger.info(f"Scheduled audit running (interval: {interval}m)...")
            is_scanning = True
            collector.run_collection()
            is_scanning = False
        except Exception as e:
            logger.error(f"Error in periodic audit: {e}")
            is_scanning = False

        # Sleep for configured interval
        await asyncio.sleep(interval * 60)

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(periodic_audit_task())

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Web UI se inicializuje... Obnovte stránku za chvíli.</h1>")
    return HTMLResponse(index_file.read_text(encoding="utf-8"))

@app.get("/api/status")
async def get_status(request: Request):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1")
    snap = c.fetchone()
    
    # Calculate live counts of ignored and actionable offline devices
    c.execute("SELECT COUNT(*) FROM devices WHERE is_online = 0 AND is_ignored = 1")
    ignored_offline = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM devices WHERE is_online = 0 AND (is_ignored = 0 OR is_ignored IS NULL)")
    actionable_offline = c.fetchone()[0]

    # Coordinator name & model if available
    c.execute("SELECT friendly_name, model_id FROM devices WHERE device_type = 'Coordinator' LIMIT 1")
    coord_row = c.fetchone()
    coord_name = coord_row["friendly_name"] if coord_row and coord_row["friendly_name"] else "SLZB-06M (192.168.74.11)"
    
    conn.close()
    
    anomalies = get_network_anomalies(hours=48)
    last_radio_scan = get_last_radio_scan_timestamp()
    last_quick_audit = snap["timestamp"] if snap else None
    is_ing = bool(request.headers.get("X-Ingress-Path") or request.headers.get("X-Hassio-Key") or os.getenv("SUPERVISOR_TOKEN"))

    if not snap:
        return {
            "status": "no_data",
            "is_scanning": is_scanning,
            "is_ingress": is_ing,
            "coordinator": coord_name,
            "anomalies": anomalies,
            "last_quick_audit": None,
            "last_radio_scan": last_radio_scan
        }
    
    snap_dict = dict(snap)
    snap_dict["ignored_offline_count"] = ignored_offline
    snap_dict["actionable_offline_count"] = actionable_offline
    
    return {
        "status": "ok",
        "is_scanning": is_scanning,
        "is_ingress": is_ing,
        "coordinator": coord_name,
        "snapshot": snap_dict,
        "anomalies": anomalies,
        "last_quick_audit": last_quick_audit,
        "last_radio_scan": last_radio_scan
    }


@app.get("/api/devices")
async def get_devices(
    type: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None
):
    conn = get_connection()
    c = conn.cursor()
    
    query = "SELECT * FROM devices WHERE 1=1"
    params = []
    
    if type and type != "all":
        query += " AND device_type = ?"
        params.append(type)
        
    if status == "online":
        query += " AND is_online = 1"
    elif status == "offline":
        query += " AND is_online = 0"
    elif status == "offline_actionable":
        query += " AND is_online = 0 AND (is_ignored = 0 OR is_ignored IS NULL)"
    elif status == "ignored":
        query += " AND is_ignored = 1"
    elif status == "weak_lqi":
        query += " AND is_online = 1 AND linkquality < 50 AND device_type != 'Coordinator'"
    elif status == "low_battery":
        query += " AND battery IS NOT NULL AND battery <= 20"
        
    if search:
        search_pattern = f"%{search.strip()}%"
        query += " AND (friendly_name LIKE ? OR ieee_addr LIKE ? OR model_id LIKE ?)"
        params.extend([search_pattern, search_pattern, search_pattern])
        
    # Order: active problems first (actionable offline), then normal online, ignored items at the end
    query += " ORDER BY is_ignored ASC, is_online ASC, linkquality ASC, friendly_name ASC"
    
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    
    devices = []
    for r in rows:
        d = dict(r)
        if d.get("raw_state"):
            try:
                d["state_parsed"] = json.loads(d["raw_state"])
            except Exception:
                d["state_parsed"] = {}
        devices.append(d)
        
    return {"devices": devices, "count": len(devices)}

@app.post("/api/devices/{ieee}/ignore")
async def toggle_device_ignore(
    ieee: str, 
    req: DeviceIgnoreRequest, 
    user: dict = Depends(require_user)
):
    success = set_device_ignore_status(ieee, req.is_ignored, req.reason)
    if not success:
        raise HTTPException(status_code=404, detail="Zařízení nenalezeno.")
    
    action = "ztišeno (očekávaný offline)" if req.is_ignored else "odtišeno (aktivní dohled)"
    return {
        "status": "ok",
        "message": f"Zařízení bylo úspěšně {action}.",
        "is_ignored": 1 if req.is_ignored else 0,
        "ignore_reason": req.reason if req.is_ignored else None
    }

@app.get("/api/events")
async def get_events(
    limit: int = 150,
    event_type: Optional[str] = None,
    hide_initial_import: bool = True,
    search: Optional[str] = None
):
    conn = get_connection()
    c = conn.cursor()
    
    initial_timestamp = None
    if hide_initial_import:
        c.execute("SELECT timestamp FROM audit_events WHERE event_type = 'DEVICE_ADDED' ORDER BY timestamp ASC, id ASC LIMIT 1")
        first_row = c.fetchone()
        if first_row:
            initial_timestamp = first_row["timestamp"]
            
    query = "SELECT * FROM audit_events WHERE 1=1"
    params = []

    if hide_initial_import and initial_timestamp:
        query += " AND NOT (event_type = 'DEVICE_ADDED' AND timestamp = ?)"
        params.append(initial_timestamp)

    if event_type and event_type != "all":
        if event_type == "drops":
            query += " AND event_type IN ('DEVICE_OFFLINE', 'DEVICE_REMOVED')"
        elif event_type == "online":
            query += " AND event_type IN ('DEVICE_ONLINE', 'IGNORED_DEVICE_BACK_ONLINE', 'CONFLICT_RESOLVED')"
        elif event_type == "battery":
            query += " AND event_type = 'LOW_BATTERY'"
        elif event_type == "mute":
            query += " AND event_type IN ('DEVICE_MUTED', 'DEVICE_UNMUTED', 'IGNORED_DEVICE_BACK_ONLINE')"
        elif event_type == "new":
            query += " AND event_type = 'DEVICE_ADDED'"
        elif event_type == "replaced":
            query += " AND event_type = 'DEVICE_REPLACED'"
        elif event_type == "blocked":
            query += " AND event_type = 'BLOCKED_DEVICE_DETECTED'"
        elif event_type == "anomalies":
            query += " AND event_type IN ('DEVICE_LEFT_NETWORK', 'DEVICE_KICKED', 'CONFLICT_RESOLVED', 'UNKNOWN_ENTITY_CALL', 'ORPHAN_CONFIG_DEVICE', 'Z2M_ERROR')"
        else:
            query += " AND event_type = ?"
            params.append(event_type)

    if search:
        search_pattern = f"%{search.strip()}%"
        query += " AND (friendly_name LIKE ? OR ieee_addr LIKE ? OR message LIKE ?)"
        params.extend([search_pattern, search_pattern, search_pattern])

    query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
    params.append(limit)

    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows], "count": len(rows)}

@app.get("/api/history/snapshots")
async def get_snapshots_history(range: str = "24h"):
    conn = get_connection()
    c = conn.cursor()
    
    now = datetime.now()
    time_filter = ""
    params = []
    if range == "24h":
        cutoff = (now - timedelta(hours=24)).isoformat()
        time_filter = "WHERE timestamp >= ?"
        params.append(cutoff)
    elif range == "7d":
        cutoff = (now - timedelta(days=7)).isoformat()
        time_filter = "WHERE timestamp >= ?"
        params.append(cutoff)
    elif range == "30d":
        cutoff = (now - timedelta(days=30)).isoformat()
        time_filter = "WHERE timestamp >= ?"
        params.append(cutoff)

    query = f"""
        SELECT id, timestamp, total_devices, routers, end_devices, 
               online_count, offline_count, weak_lqi_count, low_battery_count,
               COALESCE(ignored_count, 0) as ignored_count
        FROM snapshots 
        {time_filter}
        ORDER BY id ASC
    """
    c.execute(query, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    for r in rows:
        r["actionable_offline_count"] = max(0, r["offline_count"] - r["ignored_count"])

    return {"snapshots": rows, "count": len(rows)}

@app.get("/api/history/device/{ieee}")
async def get_device_history(ieee: str, range: str = "24h"):
    conn = get_connection()
    c = conn.cursor()
    
    c.execute("SELECT friendly_name, device_type, model_id FROM devices WHERE ieee_addr = ?", (ieee,))
    dev_info = c.fetchone()
    if not dev_info:
        conn.close()
        raise HTTPException(status_code=404, detail="Zařízení nenalezeno.")

    now = datetime.now()
    params = [ieee]
    time_filter = ""
    if range == "24h":
        cutoff = (now - timedelta(hours=24)).isoformat()
        time_filter = "AND timestamp >= ?"
        params.append(cutoff)
    elif range == "7d":
        cutoff = (now - timedelta(days=7)).isoformat()
        time_filter = "AND timestamp >= ?"
        params.append(cutoff)

    query = f"""
        SELECT timestamp, linkquality, battery, is_online
        FROM device_history
        WHERE ieee_addr = ? {time_filter}
        ORDER BY id ASC
    """
    c.execute(query, params)
    history_rows = [dict(r) for r in c.fetchall()]

    friendly_name = dev_info["friendly_name"]
    replacements = get_device_replacements(friendly_name=friendly_name) if friendly_name else []

    c.execute("SELECT * FROM hardware_blocklist WHERE ieee_addr = ?", (ieee,))
    block_row = c.fetchone()
    is_blocked = bool(block_row)
    block_info = dict(block_row) if block_row else None
    conn.close()

    return {
        "device": dict(dev_info),
        "history": history_rows,
        "count": len(history_rows),
        "replacements": replacements,
        "is_blocked": is_blocked,
        "block_info": block_info
    }

@app.get("/api/replacements")
async def get_replacements_list(friendly_name: Optional[str] = None):
    return {"replacements": get_device_replacements(friendly_name=friendly_name)}

@app.get("/api/blocklist")
async def get_blocklist_list():
    return {"blocklist": get_blocklist()}

@app.get("/api/topology")
async def get_topology(mode: str = "parents", router_ieee: Optional[str] = None, hide_offline: bool = False):
    """
    Returns nodes and edges for Vis.js Network.
    mode: 'parents' (clean tree hierarchy) or 'mesh' (full mesh including neighbor links)
    router_ieee: optional filter to focus only on one router and its children
    hide_offline: if True, completely hides offline devices from the graph
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM devices")
    devices = [dict(r) for r in c.fetchall()]
    
    c.execute("SELECT * FROM network_links")
    links = [dict(r) for r in c.fetchall()]
    conn.close()

    dev_by_ieee = {d["ieee_addr"]: d for d in devices}

    # Format nodes
    nodes = []
    for d in devices:
        ieee = d["ieee_addr"]
        name = d["friendly_name"] or ieee
        dtype = d["device_type"]
        is_online = bool(d["is_online"])
        batt = d.get("battery")
        lqi = d.get("linkquality")
        parent = d.get("parent_name") or "Neznámý"

        # Styling according to device type and state
        if dtype == "Coordinator":
            shape = "box"
            color = {"background": "#6366f1", "border": "#4338ca"}
            size = 28
        elif dtype == "Router":
            shape = "diamond"
            color = {"background": "#0284c7" if is_online else "#e11d48", "border": "#0369a1"}
            size = 20
        else: # EndDevice
            shape = "dot"
            color = {"background": "#10b981" if is_online else "#e11d48", "border": "#059669"}
            size = 12

        if not is_online:
            color = {"background": "#e11d48", "border": "#be123c"}

        title_text = (
            f"{name}\n"
            f"Typ: {dtype} ({d.get('model_id') or '-'})\n"
            f"Stav: {'ONLINE 🟢' if is_online else 'OFFLINE 🔴'}\n"
            f"LQI: {lqi if lqi is not None else '--'}\n"
            f"Baterie: {f'{batt}%' if batt is not None else '230V'}\n"
            f"Parent: {parent}"
        )

        nodes.append({
            "id": ieee,
            "label": name,
            "group": dtype,
            "shape": shape,
            "size": size,
            "color": color,
            "title": title_text,
            "is_online": is_online,
            "device_type": dtype,
            "parent_ieee": d.get("parent_ieee"),
            "parent_name": d.get("parent_name"),
            "linkquality": lqi,
            "battery": batt,
            "model_id": d.get("model_id")
        })

    # Format edges
    edges = []
    edge_set = set()

    if mode == "parents":
        # Direct parent-child connections
        for d in devices:
            p_ieee = d.get("parent_ieee")
            if p_ieee and p_ieee in dev_by_ieee:
                lqi = d.get("linkquality") or 100
                edge_color = "#10b981" if lqi >= 100 else ("#f59e0b" if lqi >= 50 else "#f43f5e")
                edges.append({
                    "from": p_ieee,
                    "to": d["ieee_addr"],
                    "label": f"{lqi}",
                    "font": {"size": 9, "color": "#94a3b8", "strokeWidth": 0},
                    "arrows": "to",
                    "color": {"color": edge_color, "highlight": "#38bdf8"},
                    "width": 1.5 if lqi >= 50 else 2.5
                })
    else:
        # Full mesh links
        for l in links:
            s = l["source_ieee"]
            t = l["target_ieee"]
            pair_key = tuple(sorted([s, t]))
            if pair_key in edge_set:
                continue
            edge_set.add(pair_key)
            if s in dev_by_ieee and t in dev_by_ieee:
                lqi = l.get("lqi") or 100
                rel = l.get("relationship", 2)
                edge_color = "#10b981" if lqi >= 100 else ("#f59e0b" if lqi >= 50 else "#f43f5e")
                edges.append({
                    "from": s,
                    "to": t,
                    "label": f"{lqi}",
                    "font": {"size": 8, "color": "#64748b", "strokeWidth": 0},
                    "color": {"color": edge_color, "highlight": "#38bdf8", "opacity": 0.5},
                    "width": 1 if rel == 2 else 2,
                    "arrows": "to" if rel in (0, 1) else ""
                })

    # Filter by router if requested
    if router_ieee:
        connected_ids = {router_ieee}
        for e in edges:
            if e["from"] == router_ieee:
                connected_ids.add(e["to"])
            elif e["to"] == router_ieee:
                connected_ids.add(e["from"])
        nodes = [n for n in nodes if n["id"] in connected_ids]
        edges = [e for e in edges if e["from"] in connected_ids and e["to"] in connected_ids]

    # Filter out offline devices if hide_offline is requested
    if hide_offline:
        nodes = [n for n in nodes if n["is_online"]]
        active_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["from"] in active_ids and e["to"] in active_ids]

    return {"nodes": nodes, "edges": edges, "total_nodes": len(nodes), "total_edges": len(edges)}

import time
from collector import ZigbeeCollector

@app.get("/api/topology/scan/status")
async def get_radio_scan_status():
    is_scanning, elapsed = collector.get_active_scan_status()
    return {
        "is_scanning": is_scanning,
        "elapsed_seconds": elapsed,
        "estimated_total_seconds": 150,
        "last_radio_scan": get_last_radio_scan_timestamp()
    }

@app.post("/api/topology/scan")
async def trigger_active_topology_scan(background_tasks: BackgroundTasks, user: dict = Depends(require_user)):
    is_scanning, elapsed = collector.get_active_scan_status()
    if is_scanning:
        return JSONResponse(
            status_code=409,
            content={"status": "busy", "message": f"Aktivní rádiový scan již probíhá (běží ~{elapsed} s). Počkejte prosím na dokončení (~2,5 minuty)."}
        )

    # Check if a scan is already running on Home Assistant directly
    ha_running, ha_msg = collector.is_active_map_scan_running()
    if ha_running:
        return JSONResponse(
            status_code=409,
            content={"status": "busy", "message": "Na Home Assistantu již právě probíhá scan spuštěný ze Zigbee2MQTT. Počkejte na dokončení."}
        )

    def do_active_scan():
        with ZigbeeCollector._active_scan_lock:
            ZigbeeCollector._active_scan_running = True
            ZigbeeCollector._active_scan_started_at = time.time()
        try:
            logger.info("Triggering active networkmap scan via MQTT...")
            collector.trigger_active_map_scan()
            # Initial grace period for coordinator to start querying
            time.sleep(15)
            # Poll HA for completion dynamically (up to 180s total)
            start_wait = time.time()
            while time.time() - start_wait < 180:
                time.sleep(5)
                ha_run, _ = collector.is_active_map_scan_running()
                if not ha_run:
                    logger.info("Active networkmap scan detected as completed on HA.")
                    break

            # Ingest freshly generated networkmap
            collector.run_collection()
            set_last_radio_scan_timestamp(datetime.now(timezone.utc).isoformat())
            logger.info("Collection after active scan completed successfully.")
        except Exception as e:
            logger.error(f"Error during active map scan: {e}")
        finally:
            with ZigbeeCollector._active_scan_lock:
                ZigbeeCollector._active_scan_running = False
                ZigbeeCollector._active_scan_started_at = 0.0

    background_tasks.add_task(do_active_scan)
    return {
        "status": "started",
        "message": "Aktivní rádiový scan byl zahájen. Bude trvat přibližně 2,5 minuty. Po dokončení se mapa automaticky aktualizuje."
    }

@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    global is_scanning
    if is_scanning:
        return {"status": "busy", "message": "Audit již probíhá."}
    
    def do_scan():
        global is_scanning
        is_scanning = True
        try:
            collector.run_collection()
        finally:
            is_scanning = False
            
    background_tasks.add_task(do_scan)
    return {"status": "started", "message": "Audit sítě spuštěn na pozadí."}

@app.get("/api/export/ai-prompt", response_class=PlainTextResponse)
async def export_ai_prompt():
    return get_condensed_summary()

@app.get("/api/export/technician", response_class=PlainTextResponse)
async def export_technician():
    return generate_technician_checklist()

@app.post("/api/ai/ask")
async def ask_ai(req: QuestionRequest):
    answer = await ask_ai_consultant(req.question, req.api_key)
    return {"answer": answer}

@app.get("/api/settings")
async def get_settings():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    settings = {row["key"]: row["value"] for row in c.fetchall()}
    conn.close()
    
    api_key = settings.get("gemini_api_key", "")
    masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else ("Nastaveno" if api_key else "")
    
    return {
        "interval_minutes": int(settings.get("interval_minutes", 5)),
        "gemini_api_key_set": bool(api_key),
        "gemini_api_key_masked": masked_key
    }

@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    conn = get_connection()
    c = conn.cursor()
    if req.interval_minutes is not None:
        c.execute("""
            INSERT INTO settings (key, value) VALUES ('interval_minutes', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (str(req.interval_minutes),))
    if req.gemini_api_key is not None:
        c.execute("""
            INSERT INTO settings (key, value) VALUES ('gemini_api_key', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (req.gemini_api_key,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": "Nastavení uloženo."}

# ==================== AUTH & USER MANAGEMENT ====================

@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response):
    conn = get_connection()
    c = conn.cursor()
    uname = req.username.strip()
    c.execute("SELECT * FROM users WHERE username = ?", (uname,))
    row = c.fetchone()
    if not row and uname == "jarek":
        c.execute("SELECT * FROM users WHERE username = 'technik'")
        row = c.fetchone()
    elif not row and uname == "technik":
        c.execute("SELECT * FROM users WHERE username = 'jarek'")
        row = c.fetchone()
    conn.close()
    if not row or not verify_password(req.password, row["salt"], row["password_hash"]):
        raise HTTPException(status_code=401, detail="Neplatné uživatelské jméno nebo heslo.")
    token = create_user_session(row["username"])
    response.set_cookie(
        key="session_token",
        value=token,
        max_age=72 * 3600,
        httponly=True,
        samesite="lax"
    )
    return {"status": "ok", "token": token, "username": row["username"], "role": row["role"]}

@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        delete_user_session(token)
    response.delete_cookie(key="session_token")
    return {"status": "ok", "message": "Byl jste úspěšně odhlášen."}

@app.get("/api/auth/me")
async def get_me(user: Optional[dict] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Nepřihlášen")
    return {
        "authenticated": True,
        "username": user["username"],
        "role": user["role"],
        "is_ingress": user.get("is_ingress", False)
    }


@app.get("/api/users")
async def list_users(user: dict = Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Pouze administrátor může spravovat uživatele.")
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, username, role, created_at FROM users ORDER BY id ASC")
    users = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"users": users}

@app.post("/api/users")
async def create_user(req: CreateUserRequest, user: dict = Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Pouze administrátor může přidávat uživatele.")
    if len(req.username.strip()) < 3:
        raise HTTPException(status_code=400, detail="Uživatelské jméno musí mít alespoň 3 znaky.")
    if len(req.password) < 4:
        raise HTTPException(status_code=400, detail="Heslo musí mít alespoň 4 znaky.")
    pwd_hash, salt = hash_password(req.password)
    now_str = datetime.now().isoformat()
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO users (username, password_hash, salt, role, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (req.username.strip(), pwd_hash, salt, req.role, now_str))
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Uživatel s tímto jménem již existuje.")
    return {"status": "ok", "message": f"Uživatel {req.username} byl úspěšně vytvořen."}

@app.delete("/api/users/{username}")
async def delete_user(username: str, user: dict = Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Pouze administrátor může mazat uživatele.")
    if username == user["username"]:
        raise HTTPException(status_code=400, detail="Nemůžete smazat svůj vlastní přihlášený účet.")
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Uživatel {username} byl smazán."}

@app.post("/api/users/change-password")
async def change_password(req: ChangePasswordRequest, user: dict = Depends(require_user)):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (user["username"],))
    row = c.fetchone()
    if not row or not verify_password(req.old_password, row["salt"], row["password_hash"]):
        conn.close()
        raise HTTPException(status_code=400, detail="Původní heslo není správné.")
    if len(req.new_password) < 4:
        conn.close()
        raise HTTPException(status_code=400, detail="Nové heslo musí mít alespoň 4 znaky.")
    new_hash, new_salt = hash_password(req.new_password)
    c.execute("""
        UPDATE users SET password_hash = ?, salt = ? WHERE username = ?
    """, (new_hash, new_salt, user["username"]))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": "Heslo bylo úspěšně změněno."}

class ResetPasswordRequest(BaseModel):
    username: str
    new_password: str

@app.post("/api/users/reset-password")
async def reset_password(req: ResetPasswordRequest, user: dict = Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Pouze administrátor může resetovat heslo.")
    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="Nové heslo musí mít alespoň 4 znaky.")
    new_hash, new_salt = hash_password(req.new_password)
    target_username = req.username.strip()
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?", (new_hash, new_salt, target_username))
    if c.rowcount == 0 and target_username in ("jarek", "technik"):
        fallback_target = "technik" if target_username == "jarek" else "jarek"
        c.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?", (new_hash, new_salt, fallback_target))
        if c.rowcount > 0:
            target_username = fallback_target
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Heslo uživatele {target_username} bylo změněno."}

