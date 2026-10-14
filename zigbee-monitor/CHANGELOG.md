# Changelog

Všechny významné změny v tomto doplňku budou dokumentovány v tomto souboru.

## [1.0.4] - 2026-10-14
### Přidáno & Opraveno
- **Automatická detekce MQTT (Zero-Config)**: Monitor automaticky přebírá přihlašovací údaje k MQTT brokeru (`user`, `password`, `server`, `topic`) přímo z existujícího souboru `configuration.yaml` Zigbee2MQTT. Není nutné nic ručně zadávat v nastavení doplňku.
- **Okamžitá validace před spuštěním rádiového scanu**: Před zahájením scanu se okamžitě ověří odeslání MQTT požadavku. Pokud broker požadavek odmítne (např. chybné heslo), uživatel ihned vidí srozumitelnou chybovou hlášku místo tichého selhání.
- **Indikátor stavu MQTT komunikace v záhlaví**: V horní liště vedle koordinátora přidán stavový odznak `MQTT: OK` (zelená) / `MQTT: CHYBA` (červená) s tooltipem diagnostiky spojení.
- **Oprava načítání času posledního scanu při rotaci logů**: Opraven výběr nejnovějšího scanu porovnáváním časových razítek v logu místo abecedního řazení souborů `log*.log`.

## [1.0.3] - 2026-10-14
### Opraveno & Vylepšeno
- **Kontinuita topologie při výpadku/timeoutu routerů**: Pokud během aktivního rádiového scanu některý router neodpoví včas na `Mgmt_Lqi_req` (timeout LQI), koncová bateriová zařízení (např. TRV hlavice) neztratí své vazby v topologické mapě. Monitor nyní zachovává posledního známého platného rodiče z databáze (`parent persistence`).
- **Informativní nápověda pro neznámé rodiče**: Pokud zařízení ještě nemá záznam o rodiči v databázi, zobrazuje se přehledný tooltip vysvětlující, že zařízení je v síti online, ale router neodpověděl při scanu.

## [1.0.2] - 2026-10-14
### Opraveno
- **Plná obousměrná synchronizace blocklistu**: Při odebrání adresy ze Z2M `configuration.yaml` (nebo jeho vyprázdnění na `blocklist: []`) se záznam okamžitě automaticky odstraní i z evidence monitoru.
- **Manuální správa blocklistu**: Do tabulky Blocklistu přidáno tlačítko pro rychlé smazání zařízení z evidence.

## [1.0.1] - 2026-10-14

### Změněno & Vylepšeno
- **Úspora místa v záložce Přehled**: Informace o koordinátorovi sítě a celkové zdraví sítě přesunuty přímo do horní lišty. Odstraněn duplicitní stavový pruh.
- **Automatické obnovování (Auto-reload)**: Zaveden periodický refresh metrik každých 30 sekund s indikátorem stavu a okamžitým obnovením při návratu do okna.
- **Optimalizace pro Home Assistant Ingress**: V Ingress režimu se automaticky skrývá správa uživatelských účtů a tlačítko odhlášení (autentizace je kompletně delegována na Home Assistant).

## [1.0.0] - 2026-10-14

### Přidáno
- Úvodní veřejná verze univerzálního Zigbee Network Monitoru.
- Podpora pro Home Assistant Ingress a automatické SSO přihlášení.
- Multi-mode sběr telemetrie: `local` (z disku), `mqtt` a `ssh`.
- Interaktivní topologická mapa Vis.js s LQI barevným rozlišením a filtry.
- Evidence výměny baterií a hardware s kalkulací odhadované životnosti.
- Detekce ZDO Leave a anomálií signálu v reálném čase.
- Generování servisního checklistu pro technika a konzultant Google Gemini.
- Nastavitelná retence dat (120 dní) v SQLite s optimalizovanými B-Tree indexy.
