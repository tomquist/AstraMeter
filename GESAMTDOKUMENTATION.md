# ASTRA – Gesamtdokumentation der Speichersteuerung

Stand: 12. Oktober 2026

## 1. Ziel und Ergebnis

ASTRA koordiniert einen FoxESS-Hausspeicher und zwei Marstek-AC-Speicher an
einem gemeinsamen Netzanschluss. Der Shelly Pro 3EM ist die einzige Referenz
für Bezug und Einspeisung. Damit nicht mehrere unabhängige Regler gegeneinander
arbeiten, werden schnelle Marstek-Regelung und langsame FoxESS-Priorisierung
klar getrennt.

Die gewünschte Reihenfolge ist umgesetzt:

- Laden: zuerst FoxESS, danach Marstek V2 und V3.
- Entladen: zuerst Marstek V2 und V3, FoxESS zuletzt.
- Zeitverzögerungen, Schwellwerte und Zustandsverriegelungen verhindern häufige
  Moduswechsel und gegenseitiges Laden.

## 2. Systemübersicht

```text
                         Heimnetz / WLAN
                                │
          ┌─────────────────────┼──────────────────────┐
          │                     │                      │
 Shelly Pro 3EM            ESP32 / ASTRA       Marstek V2 und V3
 10.0.40.72                10.0.40.74          10.0.40.85 / .77
 Netzfluss L1–L3      ┌─────────┴─────────┐      ▲ CT002 über WLAN
                      │                   │      │
                 CT002-Emulation    Modbus RTU  │
                      │              UART2/RS485 │
                      └───────────────────┐      │
                                          ▼      │
                                  FoxESS H3-6.0-E
                                  Slave-ID 247
```

Der FoxESS-Cloud-Connector unter `10.0.40.69` ist für die lokale Steuerung nicht
erforderlich. Der ESP32 spricht den Wechselrichter direkt über Modbus RTU an.

## 3. Geräte und Adressen

| Gerät | Adresse / Kennung | Aufgabe |
|---|---|---|
| ESP32-D0WDQ6, 4 MB | `10.0.40.74`, zuletzt `COM7` | ASTRA, CT002, Modbus und Weboberfläche |
| Shelly Pro 3EM | `10.0.40.72` | Dreiphasige Messung des gesamten Netzanschlusses |
| Marstek Venus E 2.0, `MST_ACCP_V2` | `10.0.40.85`, Consumer `009b08782ad6` | AC-Speicher |
| Marstek Venus E 3.0 | `10.0.40.77`, Consumer `0c587b17db18` | AC-Speicher |
| Gemeinsamer virtueller CT | MAC `02b25069325f`, HME-4/CT002 | Messquelle für beide Marstek-Speicher |
| FoxESS H3-6.0-E | Modbus RTU, Slave `247` | Hybridwechselrichter und primärer Speicher |
| FoxESS Cloud Connector | `10.0.40.69` | Hersteller-Cloud; nicht Teil der lokalen Regelung |

Die Marstek-App kann wegen der unterschiedlichen Gerätegenerationen nur einen
Speicher in `Venus-Group-325f` anzeigen. ASTRA hat dennoch beide CT002-Consumer
mit Abfragen etwa alle 0,5 bis 0,7 Sekunden erkannt.

## 4. Hardware und Verdrahtung

Der FoxESS ist über einen MAX485-Adapter am zweiten Hardware-UART des ESP32
angeschlossen.

| ESP32 | MAX485 | Funktion |
|---|---|---|
| GPIO16 / RX2 | RO | Empfang vom RS485-Adapter |
| GPIO17 / TX2 | DI | Senden zum RS485-Adapter |
| GPIO5 | DE und /RE gemeinsam | Sende-/Empfangsrichtung |
| 3V3 | VCC | Versorgung des eingesetzten Moduls |
| GND | GND | Gemeinsame Logikmasse |
| — | A / B | Separate RS485-Leitung zum FoxESS |

Serielle Parameter: `9600 Baud`, `8 Datenbits`, keine Parität, `1 Stopbit`
(`8N1`). Der vorhandene FoxESS-Zähler bleibt an seinem eigenen Anschluss; der
ESP32 wird nicht parallel auf dessen Bus betrieben.

Wichtige Hardwarehinweise:

- GPIO5 ist ein Strapping-Pin. Der installierte Aufbau startet zuverlässig,
  Änderungen am Adapter oder an Pull-up/Pull-down-Widerständen können den Boot
  jedoch beeinflussen.
- Der klassische MAX485 ist offiziell für 5 V vorgesehen. Der vorhandene kurze
  Bus funktioniert bei 3,3 V, liegt damit aber außerhalb der Spezifikation.
  Bei Kommunikationsfehlern sollte er durch MAX3485 oder SP3485 ersetzt werden.

## 5. Mess- und Kommunikationswege

### Shelly

ASTRA liest jede Sekunde lokal:

```text
http://10.0.40.72/rpc/EM.GetStatus?id=0
```

Verwendet werden `a_act_power`, `b_act_power` und `c_act_power`. Netzbezug ist
positiv, Einspeisung negativ. Der Shelly erfasst bereits FoxESS und beide
Marstek-Speicher; FoxESS-Leistung darf deshalb nicht nochmals zur Messung
addiert werden.

### Marstek

Der ESP32 emuliert einen gemeinsamen CT002/HME-4. Beide Marstek-Speicher sind
mit diesem CT verbunden und werden von der vorhandenen AstraMeter-Regelung
verteilt. Unterhalb der Effizienzschwelle von 150 W kann absichtlich nur ein
Marstek arbeiten. Bei höherem Bedarf wird die Leistung fair verteilt.

### FoxESS

ASTRA verwendet folgende lokale Modbus-Register:

| Register | Bedeutung | Zugriff |
|---:|---|---|
| 31000 | PV1-Spannung, Faktor 0,1 V | Lesen |
| 31036 | Batterieleistung; negativ bedeutet Laden | Lesen |
| 31038 | Batterie-SOC in Prozent | Lesen |
| 41000 | Arbeitsmodus: `0` Self Use, `2` Backup | Lesen und Schreiben |
| 41011 | Minimum SOC on Grid | Nur Lesen |

Der Schreibzugriff auf Register 41000 wurde mit `Self Use → Backup → Self Use`
reversibel getestet und jeweils per Rücklesen bestätigt. Register 41011 blieb
bei 10 % und wird von ASTRA grundsätzlich nicht verändert.

## 6. Regelstrategie

### Laden

1. FoxESS steht auf `Self Use` und erhält die verfügbare PV-Energie zuerst.
2. Solange der FoxESS-SOC unter 97 % liegt, blendet ASTRA Einspeisung gegenüber
   CT002 aus. Die Marstek-Speicher können dadurch nicht aus FoxESS laden.
3. Ab 97 % FoxESS-SOC und mindestens zwei Minuten Einspeisung über 150 W wird
   das Laden der Marstek-Speicher freigegeben.
4. Fällt FoxESS später unter 92 %, beginnt wieder die FoxESS-Ladepriorität.

### Entladen

1. FoxESS wird auf `Backup` gestellt; Marstek V2 und V3 decken den Verbrauch
   zuerst über die schnelle CT002-Regelung.
2. Bleibt trotz Marstek-Unterstützung länger als drei Minuten ein Netzbezug von
   über 300 W bestehen, wechselt FoxESS auf `Self Use` und entlädt zuletzt.
3. Erst nach zwei Minuten erneuter Einspeisung über 150 W beginnt ein neuer
   Ladezyklus.

### Schutz gegen Oszillation

- FoxESS wird nicht auf eine schnell wechselnde Leistungsvorgabe geregelt,
  sondern nur langsam zwischen Self Use und Backup umgeschaltet.
- Ladefreigabe und Rücknahme verwenden unterschiedliche SOC-Grenzen
  (97 % / 92 %).
- Zeitbedingungen müssen durchgehend erfüllt sein; kurze Last- oder
  PV-Spitzen lösen keinen Zustandswechsel aus.
- Jeder Moduswechsel wird zurückgelesen. ASTRA wiederholt einen fehlgeschlagenen
  Befehl zweimal und beendet automatische Schreibzugriffe bei ausbleibender
  Bestätigung.

## 7. Zustände und Fehlersicherheit

Der Sensor `ASTRA Storage Priority` zeigt einen dieser Zustände:

| Anzeige | Bedeutung |
|---|---|
| `Starting` | Eingangsdaten werden nach dem Start gesammelt |
| `FoxESS charging first` | FoxESS lädt zuerst; Marstek-Laden gesperrt |
| `Marstek charging` | FoxESS ist voll genug; Marstek darf laden |
| `Marstek discharging first` | FoxESS ist in Backup; Marstek entlädt zuerst |
| `FoxESS discharging last` | Marstek reicht nicht aus; FoxESS ist freigegeben |
| `Fault - automatic writes stopped` | Automatische FoxESS-Schreibzugriffe sind gestoppt |

Bei ungültigen oder veralteten Shelly-/FoxESS-Daten stoppt ASTRA weitere
FoxESS-Schreibzugriffe. Die unveränderten Shelly-Messwerte werden wieder an
CT002 weitergereicht. Ein Fehlerzustand wird durch einen Neustart des ESP32
zurückgesetzt, nachdem die Fehlerursache behoben wurde.

## 8. Weboberflächen und Betrieb

- ESPHome-Sensorseite: `http://10.0.40.74/`
- AstraMeter-Dashboard: `http://10.0.40.74/astrameter`
- Alternativ bei geänderter DHCP-Adresse: `http://astrameter-ct002.local/`

Das Laufzeit-Logging steht auf `INFO`. Dadurch bleiben Zustandswechsel und
Fehler sichtbar, während die sekündlichen HTTP- und Sensor-Debugmeldungen das
Dashboard nicht mehr füllen.

Unter **Each battery's effect on the grid** erscheint FoxESS als schraffierte
`external`-Spur mit Leistung, SOC und Betriebsart. Der Wert ist eine aus der
FoxESS-Batterieleistung abgeleitete Netzwirkung. Er dient nur der Anzeige;
FoxESS wird dadurch weder zum CT002-Consumer noch Teil des schnellen
Marstek-Balancers.

## 9. Firmware warten und installieren

Die produktive Konfiguration ist `astrameter-ct002.yaml`. WLAN-Zugangsdaten
liegen ausschließlich in der von Git ignorierten `secrets.yaml`.

```powershell
uv tool run --from esphome esphome config astrameter-ct002.yaml
uv tool run --from esphome esphome compile astrameter-ct002.yaml
uv tool run --from esphome esphome upload astrameter-ct002.yaml --device 10.0.40.74
```

OTA ist der normale Aktualisierungsweg. Falls OTA nicht erreichbar ist, kann
über USB geflasht werden; vorher den aktuellen COM-Port prüfen.

## 10. Diagnose

### Dashboard nicht erreichbar

1. `ping 10.0.40.74` beziehungsweise `Test-Connection 10.0.40.74` ausführen.
2. TCP-Port 80 und für OTA Port 3232 prüfen.
3. `astrameter-ct002.local` testen, falls sich die DHCP-Adresse geändert hat.
4. ESP32-Versorgung, WLAN und GPIO5/RS485-Adapter prüfen, falls das Gerät nicht
   startet.

### Keine FoxESS-Werte

1. Versorgung, GND sowie A/B prüfen; bei völliger Funkstille A und B tauschen.
2. Baudrate 9600, 8N1 und Slave-ID 247 kontrollieren.
3. Sicherstellen, dass nur der ESP32 Master auf diesem separaten RS485-Bus ist.
4. Bei sporadischen Fehlern den MAX485 durch einen echten 3,3-V-Transceiver
   ersetzen.

### Marstek reagiert nicht

1. Prüfen, ob beide Geräte mit CT `02b25069325f` verbunden sind.
2. Automatik-/CT-Betriebsmodus in der Marstek-App kontrollieren.
3. Im AstraMeter-Dashboard die Anzahl aktiver Consumer prüfen.
4. Bei kleinem Leistungsbedarf berücksichtigen, dass ASTRA aus Effizienzgründen
   absichtlich nur einen Marstek einsetzen kann.

### Unerwartete Speicherreihenfolge

Den Sensor `ASTRA Storage Priority`, FoxESS-SOC, FoxESS-Arbeitsmodus und die
Summe der drei Shelly-Phasen gemeinsam prüfen. Ein Zustandswechsel erfolgt erst
nach Ablauf der dokumentierten Zeitbedingung.

## 11. Rückfall und manuelle Wiederherstellung

Der sichere FoxESS-Rückfall ist Arbeitsmodus `Self Use` (`0`). ASTRA verändert
keine Mindest-SOC-Grenze. Bei einer Störung:

1. ESP32 ausschalten oder die ASTRA-Firmware ohne FoxESS-Automatik installieren.
2. FoxESS in der Hersteller-App auf Self Use stellen.
3. Kontrollieren, dass Minimum SOC on Grid weiterhin dem Benutzerwert entspricht.
4. Die Marstek-Speicher können unabhängig davon weiterhin über ihren CT-/App-
   Modus betrieben oder gestoppt werden.

## 12. Dateien und Versionsstand

| Datei | Inhalt |
|---|---|
| `astrameter-ct002.yaml` | Produktive ESPHome-Konfiguration |
| `foxess_priority.h` | Zustandsmaschine für die Speicherpriorität |
| `foxess_priority_test.cpp` | Kleine, eigenständige Logiktests |
| `ASTRAMETER-DEPLOYMENT.md` | Inbetriebnahme- und Verifikationsprotokoll |
| `secrets.yaml.example` | Muster ohne echte Zugangsdaten |

Implementierter Git-Stand der Prioritätssteuerung: Commit `9142a93` auf Branch
`ronny/esp32-shelly-setup`.

## 13. Noch ausstehende Langzeitprüfung

- Einen vollständigen sonnigen Ladezyklus beobachten und den Übergang bei
  97 % sowie die Rückkehr unter 92 % bestätigen.
- Mit beiden Marstek-Speichern oberhalb von 25–30 % eine kontrollierte Last von
  500–1000 W zuschalten und die gemeinsame Entladung prüfen.
- Über mehrere Tage kontrollieren, dass keine Energie zwischen FoxESS und
  Marstek pendelt und die gewählten Zeit-/Leistungsschwellen zum Haushalt
  passen.
- Für eine dauerhaft stabile URL die ESP32-Adresse im DHCP reservieren.

Diese Langzeitprüfungen betreffen die Feinabstimmung. Modbus-Kommunikation,
reversibler FoxESS-Moduswechsel, CT002-Verbindung beider Marstek-Speicher,
Firmware-Kompilierung und OTA-Installation wurden bereits erfolgreich geprüft.
