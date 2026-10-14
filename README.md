# Zigbee Network Monitor & AI Assistant

Univerzální monitorovací a diagnostický systém pro sítě **Zigbee2MQTT**.  
Umožňuje přehlednou telemetrii zařízení (LQI signál, baterie, online/offline stav), interaktivní topologickou síťovou mapu (Vis.js), evidenci změn a incidentů v čase, export servisního plánu pro technika a integrovaného AI konzultanta.

Systém je navržen tak, aby mohl běžet ve **3 různých režimech**:
1. 🏠 **Home Assistant Add-on** (s přímou integrací do bočního panelu přes Ingress)
2. 🐳 **Docker / LXC kontejner** (např. v Proxmoxu, na NAS nebo samostatném serveru)
3. 🖥️ **Samostatná Python služba** (přes lokální soubory nebo vzdálený SSH dohled)

---

## 1. Instalace jako Home Assistant Add-on

Doplněk běží přímo uvnitř Home Assistantu a integruje se do levého menu (Ingress):

1. V Home Assistantu otevřete **Nastavení** -> **Doplňky** -> **Obchod s doplňky** (*Add-on Store*).
2. Vpravo nahoře klikněte na tři tečky `⋮` -> **Repozitáře** (*Repositories*).
3. Vložte URL adresu vašeho repozitáře a klikněte na **Přidat**.
4. V seznamu vyhledejte **Zigbee Network Monitor** a klikněte na **Instalovat**.
5. V záložce **Konfigurace** můžete upravit parametry (přednastaveno pro standardní Z2M):
   * `source_mode`: `local` (čte data přímo z `/config/zigbee2mqtt`)
   * `mqtt_host`: `core-mosquitto` (interní Mosquitto broker v HA)
   * `gemini_api_key`: *(volitelně)* váš Google Gemini API klíč pro AI diagnostika.
6. Zapněte volbu **Zobrazit v bočním panelu** a klikněte na **Spustit**.

---

## 2. Spuštění v LXC kontejneru / Dockeru (např. doma v Proxmoxu)

Pokud máte Zigbee2MQTT spuštěné v samostatném LXC kontejneru nebo Dockeru mimo Home Assistant:

### Varianta A: Docker Compose
V kořenovém adresáři je připraven soubor `docker-compose.yml`:

```yaml
version: '3.8'

services:
  zigbee-monitor:
    build:
      context: ./zigbee-monitor
      dockerfile: Dockerfile
    image: zigbee-network-monitor:latest
    container_name: zigbee-network-monitor
    restart: unless-stopped
    ports:
      - "8765:8765"
    volumes:
      - ./data:/data
      # Pokud máte složku Z2M na disku:
      # - /cesta/k/zigbee2mqtt/data:/config/zigbee2mqtt:ro
    environment:
      - SOURCE_MODE=local
      - Z2M_PATH=/config/zigbee2mqtt
      - SCAN_INTERVAL_MINUTES=5
      - MQTT_HOST=192.168.1.50 # IP vašeho MQTT brokera
      - MQTT_PORT=1883
      - MQTT_TOPIC=zigbee2mqtt
      - GEMINI_API_KEY=
```

Spusťte příkazem:
```bash
docker compose up -d
```
Aplikace je dostupná na `http://<IP-kontejneru>:8765`.

---

## 3. Režimy sběru dat (Multi-Mode)

| Režim | Kdy použít | Zdroj dat | Odesílání radio scanu |
| :--- | :--- | :--- | :--- |
| **`local`** *(doporučeno)* | HA Add-on nebo LXC se sdíleným diskem | Čte bleskově z disku (`database.db`, `state.json`, logy) | Nativní MQTT (`paho-mqtt`) |
| **`mqtt`** | Z2M v izolovaném LXC bez přístupu k disku | MQTT telemetrie | Nativní MQTT (`paho-mqtt`) |
| **`ssh`** | Vzdálený dohled na jiný server | Dávkový SSH čtecí skript | `mosquitto_pub` přes SSH |

---

## 4. Klíčové funkce

* **0 % zátěž Zigbee rádia při běžném dohledu:** Pasivní čtení stavů a logů bez vysílání jediného rádiového paketu.
* **Aktivní Network Map Scan na vyžádání:** Získání kompletní LQI matice sousedství se 3 úrovněmi ochrany proti přetížení rádia.
* **Interaktivní mapa topologie (Vis.js):** Grafická vizualizace koordinátora, routerů a koncových zařízení s filtrem kvality signálu (LQI).
* **Historie a trendy:** 120denní retence metrik s bleskovými B-Tree indexy v SQLite.
* **Správa ignorovaných zařízení & výměn baterií:** Evidence rezervních zásuvek, vyloučených čidel a výměn baterií/kusů.
* **AI Diagnostik & Servisní checklist:** Export optimalizovaného promptu (~300 tokenů) nebo přímá konzultace přes Google Gemini.

---

## 5. Struktura projektu

```
zigbee-network-monitor/
├── repository.yaml                  # Manifest Home Assistant Add-on repozitáře
├── docker-compose.yml               # Konfigurace pro LXC / samostatný Docker
├── .gitignore
├── README.md
└── zigbee-monitor/                  # Balíček doplňku & aplikace
    ├── config.yaml                  # HA Add-on manifest (Ingress, možnosti, schema)
    ├── Dockerfile                   # Produkční Dockerfile (Alpine Python)
    ├── run.sh                       # Startovací skript (načítá options.json z HA)
    ├── requirements.txt             # Python závislosti (FastAPI, paho-mqtt...)
    ├── app.py                       # FastAPI REST API & Ingress SSO podpora
    ├── collector.py                 # Multi-mode datový sběrač (Local / MQTT / SSH)
    ├── database.py                  # SQLite databáze s indexy a retencí
    ├── ai_service.py                # AI konzultant a servisní exporty
    └── static/
        ├── index.html               # Web UI (přizpůsobené pro Ingress relativní cesty)
        └── chart.min.js             # Knihovna grafů
```
