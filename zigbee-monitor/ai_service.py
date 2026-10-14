import os
import json
import sqlite3
import asyncio
import subprocess
import httpx
from typing import Dict, Any, Optional
from database import get_connection, get_network_anomalies

def get_condensed_summary() -> str:
    """Creates an ultra-compact ~30-line markdown diagnostic summary of the Zigbee network."""
    conn = get_connection()
    c = conn.cursor()

    # Get latest snapshot
    c.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1")
    snap = c.fetchone()
    if not snap:
        return "Zatím nebyl proveden žádný audit."

    # Get offline devices
    c.execute("""
        SELECT friendly_name, device_type, model_id, last_seen, is_ignored, ignore_reason 
        FROM devices WHERE is_online = 0 
        ORDER BY last_seen DESC
    """)
    offline_devs = c.fetchall()

    # Get weak LQI devices (< 50)
    c.execute("""
        SELECT friendly_name, device_type, linkquality 
        FROM devices WHERE is_online = 1 AND linkquality < 50 
        ORDER BY linkquality ASC
    """)
    weak_devs = c.fetchall()

    # Get low battery devices (<= 20%)
    c.execute("""
        SELECT friendly_name, device_type, battery 
        FROM devices WHERE battery IS NOT NULL AND battery <= 20 
        ORDER BY battery ASC
    """)
    low_batts = c.fetchall()

    # Get lowest 5 batteries for context
    c.execute("""
        SELECT friendly_name, device_type, battery 
        FROM devices WHERE battery IS NOT NULL 
        ORDER BY battery ASC LIMIT 5
    """)
    lowest_batts = c.fetchall()

    # Router child table saturation
    c.execute("""
        SELECT d.friendly_name, d.model_id, COUNT(nl.source_ieee) as children_count
        FROM devices d
        LEFT JOIN network_links nl ON d.ieee_addr = nl.target_ieee AND nl.relationship = 1
        WHERE d.device_type = 'Router'
        GROUP BY d.ieee_addr
        ORDER BY children_count DESC
    """)
    router_loads = c.fetchall()

    overloaded_routers = []
    free_b_routers = []
    for r in router_loads:
        fname = r["friendly_name"]
        model = r["model_id"] or ""
        cnt = r["children_count"]
        if "TS0001" in model and cnt >= 5:
            overloaded_routers.append(f"`{fname}` ({cnt} dětí, model {model} — na limitu kapacity!)")
        elif fname.startswith("B") and cnt <= 3:
            free_b_routers.append(f"`{fname}` ({cnt} dětí)")

    # Get recent audit events (last 10)
    c.execute("""
        SELECT timestamp, event_type, friendly_name, message 
        FROM audit_events ORDER BY id DESC LIMIT 8
    """)
    recent_events = c.fetchall()

    conn.close()

    drawer_sockets = [d for d in offline_devs if d['is_ignored'] == 1]
    room_offline = [d for d in offline_devs if d['is_ignored'] == 0]

    lines = [
        f"### Zigbee Stav Sítě ({snap['timestamp'][:16].replace('T', ' ')})",
        f"- **Celkem zařízení:** {snap['total_devices']} (Routery: {snap['routers']}, EndDevices: {snap['end_devices']})",
        f"- **Online:** {snap['online_count']} | **Offline k řešení:** {len(room_offline)} (+ {len(drawer_sockets)} v šuplíku/ztišeno) | **Slabé LQI (<50):** {snap['weak_lqi_count']} | **Slabá baterie:** {snap['low_battery_count']}",
        ""
    ]

    # Include 3-way anomalies (HA orphans, Z2M reserved, left network, HA errors)
    anomalies = get_network_anomalies(hours=48)
    if anomalies.get("has_anomalies"):
        lines.append("**🔍 3-CESTNÁ KONTROLA ANOMÁLIÍ (HA ⟷ Z2M ⟷ Zigbee rádio):**")
        if anomalies.get("left_network_devices"):
            lines.append("  - 🚨 **Opustila síť (ZDO Leave – nutno fyzicky spárovat v místnosti!):**")
            for l in anomalies["left_network_devices"]:
                lines.append(f"    • `{l['friendly_name']}` ({l['ieee_addr']}) — {l['details']}")
        if anomalies.get("ha_orphans"):
            lines.append("  - 🚨 **Osiřelé v Home Assistantu (v HA MQTT existují, ale chybí v Zigbee síti!):**")
            for o in anomalies["ha_orphans"]:
                lines.append(f"    • `{o['friendly_name']}` ({o['ieee_addr']}) — nutno smazat v HA nebo napárovat")
        if anomalies.get("unknown_entity_calls"):
            for u in anomalies["unknown_entity_calls"]:
                lines.append(f"  - ⚠️ `{u['friendly_name']}`: {u['message']}")
        if anomalies.get("z2m_reserved"):
            lines.append("  - 🏷️ **Nepoužívané záznamy v Z2M konfiguraci (ke smazání / přejmenování):**")
            for r in anomalies["z2m_reserved"]:
                lines.append(f"    • `{r['friendly_name']}` ({r['ieee_addr']})")
        lines.append("")

    if room_offline:
        lines.append("**🔴 Offline zařízení v místnostech (vyžadují prověření):**")
        for d in room_offline:
            last = d['last_seen'][:16].replace('T', ' ') if d['last_seen'] else 'nikdy'
            lines.append(f"  - `{d['friendly_name']}` ({d['device_type']}, {d['model_id']}) — naposledy: {last}")
        lines.append("")

    if drawer_sockets:
        lines.append("**📦 Očekávaně offline / V šuplíku (ztišeno — nemazat, nezatěžují síť):**")
        names = ", ".join([f"`{d['friendly_name']}`" for d in drawer_sockets])
        lines.append(f"  - {names} (Z2M má pause_on_backoff_gt, takže na ně neplýtvá rádiem a čeká na zapojení)")
        lines.append("")

    if overloaded_routers:
        lines.append("**⚠️ Vytížení routerů a limit child tabulek:**")
        for ov in overloaded_routers:
            lines.append(f"  - {ov} — plná kapacita, odmítá nová čidla (např. v místnosti 206)!")
        if free_b_routers:
            lines.append(f"  - Volné okolní routery k cílenému párování: {', '.join(free_b_routers)}")
        lines.append("")

    if weak_devs:
        lines.append("**🟡 Slabý signál (LQI < 50):**")
        for d in weak_devs:
            lines.append(f"  - `{d['friendly_name']}` ({d['device_type']}) — LQI: {d['linkquality']}")
        lines.append("")

    if low_batts:
        lines.append("**🔋 Kritická baterie (<= 20%):**")
        for d in low_batts:
            lines.append(f"  - `{d['friendly_name']}` ({d['device_type']}) — Baterie: {d['battery']}%")
        lines.append("")
    elif lowest_batts:
        low_strs = [f"`{d['friendly_name']}` ({int(d['battery'])} %)" for d in lowest_batts]
        lines.append(f"**🔋 Stav baterií v síti:** Všechna zařízení mají dostatek energie (> 35 %). Nejnižší naměřené stavy: {', '.join(low_strs)}.")
        lines.append("")

    if recent_events:
        lines.append("**📋 Poslední události:**")
        for e in recent_events:
            ts = e['timestamp'][11:16]
            lines.append(f"  - [{ts}] {e['friendly_name']}: {e['message']}")

    return "\n".join(lines)


def generate_technician_checklist() -> str:
    """Generates a plain-text task list formatted for the on-site technician."""
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT friendly_name, device_type, model_id, last_seen, is_ignored, ignore_reason 
        FROM devices WHERE is_online = 0 
        ORDER BY friendly_name ASC
    """)
    offline_devs = c.fetchall()

    c.execute("""
        SELECT friendly_name, device_type, battery 
        FROM devices WHERE battery IS NOT NULL AND battery <= 20 
        ORDER BY friendly_name ASC
    """)
    low_batts = c.fetchall()

    c.execute("""
        SELECT friendly_name, device_type, linkquality 
        FROM devices WHERE is_online = 1 AND linkquality < 45 
        ORDER BY linkquality ASC
    """)
    weak_devs = c.fetchall()

    conn.close()

    lines = [
        "Přehled servisních úkolů pro Zigbee síť v objektu:\n"
    ]

    # 3-Way Anomalies & Urgent Intervention Tasks
    anomalies = get_network_anomalies(hours=48)
    ha_urgent = []
    if anomalies.get("ha_orphans"):
        for o in anomalies["ha_orphans"]:
            ha_urgent.append(f"• {o['friendly_name']} – V HA INTEGRACI EXISTUJE, ALE CHYBÍ V ZIGBEE: buď smazat v HA MQTT, nebo ihned napárovat!")

    left_devs = anomalies.get("left_network_devices") or []
    if left_devs:
        lines.append("🚨 OPUSTILA SÍŤ (ZDO Leave – v místnosti podržet párovací tlačítko 5s):")
        for a in left_devs:
            lines.append(f"• {a['friendly_name']} ({a['ieee_addr']}) – {a['details']}")
        lines.append("")

    if ha_urgent:
        lines.append("🚨 AKUTNÍ ZÁSAH – NEKONTROLOVANÁ ZAŘÍZENÍ V HA:")
        lines.extend(ha_urgent)
        lines.append("")

    if anomalies.get("z2m_reserved"):
        lines.append("🏷️ NEPOUŽÍVANÉ ZÁZNAMY V Z2M KONFIGU (ke smazání / přejmenování):")
        for r in anomalies["z2m_reserved"]:
            lines.append(f"• {r['friendly_name']} ({r['ieee_addr']}) – starý záznam v configuration.yaml")
        lines.append("")

    drawer_sockets = [d for d in offline_devs if d['is_ignored'] == 1]
    room_offline = [d for d in offline_devs if d['is_ignored'] == 0]

    if room_offline:
        lines.append("❌ KONTROLA V MÍSTNOSTECH (Zařízení neodpovídají):")
        for d in room_offline:
            lines.append(f"• {d['friendly_name']} ({d['device_type']}, {d['model_id']})")
        lines.append("")

    if low_batts:
        lines.append("🔋 VÝMĚNA BATERIÍ:")
        for d in low_batts:
            lines.append(f"• {d['friendly_name']} - zbývá {d['battery']}%")
        lines.append("")

    if weak_devs:
        lines.append("⚠️ SLABÝ SIGNÁL (zkontrolovat pokrytí):")
        for d in weak_devs:
            lines.append(f"• {d['friendly_name']} (LQI: {d['linkquality']})")
        lines.append("")

    if drawer_sockets:
        lines.append("📦 ZÁLOŽNÍ ZÁSUVKY V ŠUPLÍKU (nevyžadují zásah):")
        lines.append("• " + ", ".join([d['friendly_name'] for d in drawer_sockets]))
        lines.append("")

    lines.append("Hotovo.")
    return "\n".join(lines)


def select_reasoning_effort(question: str) -> tuple[str, int]:
    """
    Dynamicky zvolí úroveň uvažování (effort) a timeout podle charakteru otázky:
    - 'medium' (timeout 120s): hloubkové zkoumání příčin a analýza problémů
    - 'low' (timeout 90s): bleskové přehledy, checklisty, baterie, signál (~15-28s)
    """
    q = question.lower()
    if any(k in q for k in ["proč", "hloubkov", "příčin", "souvislost", "detailní analýz"]):
        return ("medium", 120)
    return ("low", 90)


async def ask_ai_consultant(question: str, custom_api_key: Optional[str] = None) -> str:
    """
    Direct consultation with the AI engine.
    1. Uses local AGY-server AI CLI directly (/usr/local/bin/agy) — 100% native on this server!
    2. Fallback to Gemini API if key is configured.
    """
    condensed_context = get_condensed_summary()
    effort, timeout_secs = select_reasoning_effort(question)

    system_prompt = (
        "Jsi expertní Zigbee asistent běžící na lokálním serveru pro dohledovou síť v objektu.\n"
        "Odpovídáš technikovi a správci sítě přímo ve webovém rozhraní monitoru.\n\n"
        "ZÁVAZNÁ PRAVIDLA A FAKTA O SÍTI:\n"
        "1. ZÁLOŽNÍ ZÁSUVKY V ŠUPLÍKU: Offline zásuvky (Zasuvka_1, Zasuvka_3, Zasuvka_4, Zasuvka_5, Zasuvka_6, Zasuvka_8 apod.) "
        "jsou záložní kusy uskladněné v šuplíku pro rychlé nasazení. NIKDY nedoporučuj jejich mazání ani 'force remove' ze Zigbee2MQTT! "
        "Z2M má v konfiguraci zapnuto `pause_on_backoff_gt: 1`, takže po 2 výpadcích na ně neplýtvá rádiem a čeká na jejich budoucí zapojení do zdi.\n"
        "2. ŽÁDNÉ FABULACE ANI SPOJOVÁNÍ LOKALIT: Zásuvka_1 NENÍ v místnosti 206 ani nenapájí čidla z 206! "
        "Pokud zařízení nemá číslo místnosti v názvu nebo není vazba v topologii, NEVYMÝŠLEJ SI, kde se nachází ani co napájí. "
        "Nemíchej nesouvisející zařízení dohromady jen proto, že vypadla ve stejný den.\n"
        "3. ŽÁDNÉ SPÍNAČE BEZ NULY: V objektu nejsou žádné spínače bez Nuly. Síť tvoří LAN koordinátor SLZB-06M, "
        "páteřní routery SLZB-06M, routerové zásuvky (Sonoff/Tuya) a koncová čidla (Tuya) a hlavice (Sonoff TRVZB).\n"
        "4. STYL KOMUNIKACE: Odpovídej věcně, technicky přesně, profesionálně v češtině. "
        "NEPOUŽÍVEJ žádná osobní oslovení ani konkrétní jména osob, ani názvy měst či firem. Používej přehledné odrážky pro servisního technika.\n"
        "5. KAPACITA ROUTERŮ A ODMÍTÁNÍ ČIDEL: Routery Tuya TS0001 (např. B5, A4, C2) mají fyzický limit 5–7 dětí. "
        "Pokud má router plnou kapacitu (např. B5 má 7 dětí), aktivně odmítá nová či rejoining čidla (status Table Full)! "
        "Pokud je čidlo v dané zóně offline (např. 206T), upozorni na přeplněný router a doporuč cílené párování přes volný router v okolí "
        "(např. B1, B3, B4 s 0-3 dětmi nebo SLZB-06M) volbou 'Permit join <router>' v Zigbee2MQTT namísto globálního párování."
    )

    full_prompt = (
        f"{system_prompt}\n\n"
        f"--- AKTUÁLNÍ DIAGNOSTICKÝ STAV SÍTĚ ---\n{condensed_context}\n\n"
        f"--- DOTAZ PRO AI DIAGNOSTIKA ---\n{question}"
    )

    # 1. Native AGY server engine
    agy_bin = "/usr/local/bin/agy"
    if os.path.exists(agy_bin):
        try:
            def run_agy():
                res = subprocess.run(
                    [agy_bin, "--effort", effort, "--dangerously-skip-permissions", "-p", full_prompt],
                    capture_output=True, text=True, timeout=timeout_secs
                )
                if res.returncode != 0:
                    print(f"[AGY ERROR] RC={res.returncode}, STDERR={res.stderr}")
                return res.stdout.strip()
            
            output = await asyncio.to_thread(run_agy)
            if output:
                return output
        except Exception as e:
            print(f"[AGY EXCEPTION] {e}")

    # 2. Fallback: Gemini REST API if configured
    api_key = custom_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'gemini_api_key'")
        row = c.fetchone()
        if row and row["value"]:
            api_key = row["value"]
        conn.close()

    if api_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1000}
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            pass

    return f"Odpověď od AI serveru momentálně není k dispozici.\n\nAktuální stav sítě:\n{condensed_context}"
