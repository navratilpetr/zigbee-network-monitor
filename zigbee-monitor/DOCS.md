# Home Assistant Add-on: Zigbee Network Monitor

Univerzální monitorovací a diagnostický systém pro sítě **Zigbee2MQTT**.

## Přehled funkcí

- **0 % zatížení Zigbee rádia při běžném dohledu:** Pasivní čtení lokálních stavů z disku nebo přes MQTT bez vysílání rádiových zpráv.
- **Topologická síťová mapa (Vis.js):** Interaktivní vizualizace koordinátora, routerů a koncových zařízení s barevným odlišením podle LQI kvality signálu.
- **Detekce anomálií a změn:** Sledování náhlých pádů LQI, offline čidel a ZDO Leave událostí (odpojení ze sítě).
- **Správa údržby:** Evidence ignorovaných/testovacích zařízení, log výměny baterií a hardware.
- **AI Asistent:** Integrovaná analýza stavu sítě přes Google Gemini s generováním servisního checklistu pro technika.
- **Ingress integrace:** Přímý přístup z bočního panelu Home Assistantu bez nutnosti zadávat dodatečné přihlašovací údaje (Single Sign-On).

---

## Konfigurace doplňku

V záložce **Konfigurace** doplňku jsou k dispozici následující volby:

| Volba | Výchozí hodnota | Popis |
| :--- | :--- | :--- |
| `source_mode` | `local` | Režim sběru dat (`local`, `mqtt`, `ssh`). Pro HA Add-on ponechte `local`. |
| `z2m_path` | `/config/zigbee2mqtt` | Cesta k datové složce Zigbee2MQTT v Home Assistantu. |
| `scan_interval_minutes` | `5` | Interval pasivního auditu sítě v minutách. |
| `offline_threshold_hours` | `12` | Počet hodin bez komunikace, po kterých je zařízení označeno jako offline. |
| `lqi_weak_threshold` | `50` | Hranice LQI, pod kterou je signál vyhodnocen jako slabý (žlutá/červená barva). |
| `battery_low_threshold` | `20` | Procento baterie považované za nízké. |
| `retention_days` | `120` | Počet dní uchovávání historických dat a událostí v SQLite. |
| `mqtt_host` | `core-mosquitto` | Hostname MQTT brokera (výchozí pro Mosquitto Add-on v HA). |
| `mqtt_port` | `1883` | Port MQTT brokera. |
| `mqtt_user` | *(prázdné)* | Uživatelské jméno MQTT (pokud je vyžadováno). |
| `mqtt_password` | *(prázdné)* | Heslo MQTT brokera. |
| `mqtt_topic` | `zigbee2mqtt` | Kořenový MQTT topic Zigbee2MQTT. |
| `gemini_api_key` | *(prázdné)* | API klíč pro Google Gemini (volitelné pro AI asistenta). |

---

## První spuštění

1. Ujistěte se, že běží Zigbee2MQTT a jeho data jsou v `/config/zigbee2mqtt`.
2. Spusťte doplněk **Zigbee Network Monitor**.
3. Zaškrtněte **Zobrazit v bočním panelu** pro snadný přístup z levého menu Home Assistantu.
4. Otevřete webové rozhraní doplňku – systém automaticky provede první pasivní audit a zobrazí přehled vaší sítě.
