# utilmanager – Flüssiggas

Eine Flask-Anwendung für einen Haushalt: Tabellenansicht nach `Gasverbrauch.ods`,
manuelle Monatswerte und Lieferungen, lokale Fotoerkennung mit Tesseract und
SQLite. Frontend, API und OCR laufen in **einem Container**. Datenbank und Fotos
liegen auf einem persistenten Volume. Keine externen OCR-Dienste, CDNs oder
Frontend-Buildschritte.

## Enthalten

- Jahresweise Tabelle mit Verbrauch, Lieferungen, Restbestand, Füllstand,
  Vorjahresvergleich, Projektion, Bewertungspreis und Verbrauchskosten.
- Verbrauch und Lieferungen anlegen, korrigieren und ausdrücklich bestätigt löschen.
- Änderungsprotokoll; Schutz vor doppelten Monatswerten, wiederholten Formularen
  und dem Überschreiben zwischenzeitlich geänderter Einträge.
- Einmaliger ODS-Import bei der Ersteinrichtung, alternativ manueller Anfangsbestand.
- Foto-Upload per Browser oder API. OCR liefert einen **Entwurf**; erst die
  Bestätigung im Browser erzeugt einen Verbrauchseintrag. Danach wird die
  Bilddatei gelöscht, ebenso beim ausdrücklichen Verwerfen.
- Konsistentes Backup der SQLite-Datenbank einschließlich der zugehörigen Fotos.

Ein grafisches Dashboard und die Erkennung von Lieferscheinen/Rechnungen sind
nicht Teil dieser Version. Lieferungen werden manuell eingetragen.

## Mit Docker Compose starten

Im Projektverzeichnis:

```sh
python3 manage.py init-env
docker compose up -d --build
```

`init-env` benötigt nur die Python-Standardbibliothek und legt individuelle
Zugangsdaten in `.env` an. Eine vorhandene Datei wird **nicht überschrieben**.
Alternativ `.env.example` nach `.env` kopieren und die drei Geheimnisse setzen.
Jedes benötigt mindestens 16 Zeichen, empfohlen sind zufällige 32-Byte-Werte.

Die Oberfläche ist lokal unter **http://127.0.0.1:8080** erreichbar. Benutzername
und Passwort stehen in `.env` unter `APP_USER` und `APP_PASSWORD`.
Bei der ersten Anmeldung `Gasverbrauch.ods` im Browser auswählen und importieren.
Die Datei wird geprüft und atomar übernommen. Bei Fehlern wird nichts importiert.

Das Image enthält absichtlich keine private ODS-Datei oder voreingefüllte Datenbank.
Das Compose-Volume `gas-data` ist unabhängig von einem eventuell vorhandenen
lokalen `data/`-Ordner. Die Ersteinrichtung erfolgt daher pro Installation.

### Einbindung auf dem Odroid

Den Service aus `compose.yaml` in den vorhandenen Stack übernehmen und mit dem
bestehenden Reverse Proxy verbinden. Im gemeinsamen Docker-Netz ist das Ziel
`http://gasverbrauch:8000`. Die veröffentlichte Loopback-Portbindung kann dann
entfallen. Für einen Reverse Proxy direkt auf dem Host ist das Ziel
`http://127.0.0.1:8080`.

- HTTPS am Reverse Proxy verwenden; danach `COOKIE_SECURE=true` setzen.
- Den `Authorization`-Header an die App weiterreichen. Die App nutzt HTTP Basic
  für den Browser und Bearer-Authentifizierung für `/api/`.
- Upload-Limit des Proxys auf mindestens 12 MiB und Upstream-Timeout auf
  mindestens 60 Sekunden setzen.
- Das Volume auf einem **lokalen Datenträger** des Odroid betreiben, nicht auf
  SMB/NFS. Bei einem Bind Mount benötigt UID/GID `10001` Schreibrechte.
- Kein festes `platform` ist vorgegeben. Python-Basisimage, Pillow und Tesseract
  müssen zur CPU und zum Betriebssystem passen. Das konkrete Odroid-Modell ist
  noch unbekannt; der Build auf dem Gerät ist deshalb der abschließende Nachweis.

Der Container läuft als UID `10001`, mit schreibgeschütztem Root-Dateisystem,
einem beschreibbaren Datenvolume und temporärem Speicher unter `/tmp`.
Ein Gunicorn-Prozess mit zwei Threads bedient Webanfragen; die Erkennung bekommt
maximal 25 Sekunden. Es gibt weder Queue noch zusätzlichen Worker-Container.

### Betrieb unter einem Subpath

Für beispielsweise `http://aludra.fritz.box/utilmanager/` in `.env` setzen:

```dotenv
APP_BASE_PATH=/utilmanager
```

Danach `docker compose up -d --build` ausführen. Ohne diese Variable (oder mit
`APP_BASE_PATH=/`) läuft die Anwendung weiterhin unter `/`. Ein abschließender
Slash wird entfernt; verschachtelte Pfade wie `/apps/utilmanager` sind möglich.
Anzugeben ist nur der Pfad, keine vollständige URL.

Der Reverse Proxy muss Anfragen unter diesem Pfad an den Container weiterleiten.
Er kann den Präfix beibehalten oder entfernen. Beispiel für nginx im gemeinsamen
Docker-Netz, mit beibehaltenem Präfix:

```nginx
location = /utilmanager {
    return 308 /utilmanager/$is_args$args;
}
location /utilmanager/ {
    proxy_pass http://gasverbrauch:8000;
    proxy_set_header Host $http_host;
    client_max_body_size 12m;
    proxy_read_timeout 60s;
}
```

Navigation, Stylesheets, Formulare, Foto-URLs, Weiterleitungen und API-Antworten
berücksichtigen den konfigurierten Pfad. Session-Cookies gelten für diesen Pfad.
API-Clients verwenden dann beispielsweise `/utilmanager/api/uploads`.
Der interne Docker-Healthcheck unter `/healthz` funktioniert weiterhin.
Direkte Backend-Anfragen ohne Präfix bleiben für Proxys mit entferntem Präfix
möglich; ihre generierten Links enthalten ebenfalls `APP_BASE_PATH`.

## Rechenmodell und Import

Die Originaleinheiten bleiben erhalten: Lieferungen in Litern, Verbrauch in kWh.
Dezimalwerte werden als exakte Dezimalzeichenfolgen gespeichert und in Python
mit `Decimal` berechnet. Gerundet wird erst für die Anzeige.

```text
Bestand_kWh = Anfangsbestand_Liter × Faktor
            + Summe(Lieferungen_Liter × Faktor)
            − Summe(Verbrauch_kWh)
Bestand_Liter = Bestand_kWh / Faktor
Füllstand_% = Bestand_Liter / Tankvolumen_Liter × 100
```

Die mitgelieferte Quelle enthält 6.520 l Tankvolumen, den Faktor **6,57 kWh/l**
und 4.956 l Anfangsbestand vor November 2019. Beim Import werden diese Werte
aus dem ersten Blatt gelesen, nicht aus allen anderen Blättern übernommen.
81 Monatswerte und sechs Lieferungen ergeben Ende Juli 2026 einen Bestand von
**7.268,89 kWh = 1.106,375951… l**.

- Der Verbrauch gehört zum abgelesenen **Bezugsmonat**, nicht zum Fotodatum.
  Für neue Monatswerte sind nur abgeschlossene Monate erlaubt, Zeitzone Berlin.
- Nullverbrauch (`0`) bleibt ein Messwert. Eine leere Zelle bleibt fehlend.
  Insbesondere ist Juni 2025 in der Quelle mit `0` erfasst, August 2026 ist leer.
- Nach einer Lücke wird kein vollständiger Rechenbestand ausgewiesen. Die
  Projektion läuft mit dem gleichen Vorjahresmonat weiter, wenn vorhanden.
  Bei später erfassten Monaten zieht sie wieder den tatsächlichen Verbrauch ab;
  ältere Schätzanteile bleiben bis zum Nachtragen der Lücken geschätzt.
- Fehlt auch der Vorjahres-/Vergleichswert, bleibt die Projektion offen.
  Es wird kein Nullverbrauch und kein Wettermodell erfunden.
- Frühe Vergleichswerte aus Spalte H bleiben zusätzliche Referenzen für den
  jeweiligen Tabellenmonat und erzeugen keine weiteren Verbrauchsbuchungen.
- Lieferungen aus der ODS sind monatsgenau. Ein genauer Liefertag bleibt leer.
  Neue Lieferungen dürfen optional einen Tag haben. Mehrere Lieferungen pro
  Monat sind möglich.
- Spalte K enthält einen fortgeschriebenen **Bewertungspreis**. Beim Import
  werden seine Änderungen separat erhalten; die tatsächlichen Einkaufspreise
  der importierten Lieferungen bleiben unbekannt.
- Bei neuen Lieferungen kann ein Preis angegeben werden. Er gilt ab dem
  Liefermonat für die Verbrauchskosten, auch wenn darin ein historischer
  Bewertungspreis steht. Innerhalb eines Monats gewinnt die letzte Lieferung
  mit Preis, sortiert nach Datum; monatsgenaue Lieferungen stehen vor solchen
  mit bekanntem Tag, untereinander nach Erfassungsreihenfolge. In späteren
  Monaten gilt wieder ein dort explizit importierter Bewertungspreis, falls
  vorhanden. Das bildet keine FIFO- oder Mischpreisbewertung ab.
- Verbrauchskosten sind `Verbrauch_kWh / Faktor × Bewertungspreis`. Bei fehlendem
  Verbrauch werden die Kosten anhand der Projektion als Schätzung ausgewiesen.
  Es sind **keine Rechnungsbeträge** der Lieferungen.
- Tankvolumen, Faktor und Anfangsbestand sind nach der Ersteinrichtung in dieser
  Version nicht über die Oberfläche veränderbar. Damit ändern Konfigurations-
  eingriffe keine historischen Berechnungen unbemerkt.

Der Import führt keine ODS-Formeln aus. Er liest Eingaben und prüft die
nachgerechneten Bestände gegen gespeicherte Formelergebnisse. Abweichungen
über 0,01 kWh, unpassende Spalten, doppelte Monate und Formeln in der
Verbrauchsspalte führen zum Abbruch. Ein weiterer Import in einen bereits
eingerichteten Datenbestand wird abgelehnt; dieselbe Datei ist ein No-op.

## iPhone-Foto-Endpoint

```http
POST /api/uploads
Authorization: Bearer <UPLOAD_TOKEN>
Content-Type: multipart/form-data
Idempotency-Key: <optionale eindeutige ID pro Aufnahme>
```

Formularfelder:

| Feld | Inhalt |
| --- | --- |
| `image` | JPEG, PNG oder WebP; maximal 12 MiB Anfragegröße und 25 Megapixel |
| `month` | Verbrauchsmonat als `YYYY-MM`; ohne Angabe der letzte abgeschlossene Monat |

HEIC zuerst auf dem iPhone in JPEG umwandeln. Die Anwendung richtet das Bild
anhand seiner Orientierung aus, begrenzt es auf 2.400 Pixel Kantenlänge und
speichert eine JPEG-Kopie ohne EXIF-Daten. Die Originaldatei bleibt auf dem
iPhone; serverseitig wird diese normalisierte Kopie zur Prüfung aufbewahrt.

Beispielantwort (`201 Created`):

```json
{
  "id": "<upload-id>",
  "month": "2026-08",
  "status": "pending",
  "photo_available": true,
  "candidates_kwh": ["3400"],
  "warning": "",
  "review_url": "/uploads/<upload-id>"
}
```

`review_url` beginnt mit einem Slash und enthält den Subpath bereits. Zum Öffnen
Schema und Host (gegebenenfalls mit Port) davor setzen. Dort wird der Wert geprüft, bei Bedarf
korrigiert und gebucht. Mehrere OCR-Kandidaten werden nicht automatisch addiert.
Ein OCR-Fehler lässt die manuelle Prüfung des Fotos weiterhin zu.

Die OCR liest zunächst das Originalbild. Bei dunklen Displays oder fehlendem
kWh-Wert folgen zwei Schwarz-Weiß-Varianten, die helle Schrift isolieren.
Eng benachbarte, gleich große Ziffern werden anhand der Tesseract-Positionen
zusammengeführt (z. B. `1 9 5` → `195`). Verschiedene erkannte kWh-Werte bleiben
getrennte Kandidaten; sie werden weder addiert noch automatisch einem Monat
zugeordnet. Der Monat stammt weiterhin aus dem Upload-Feld `month` bzw. aus
der Vormonatsvorgabe und muss am Foto geprüft werden.

Unter **„Erkannter Text“** stehen die Ergebnisse der einzelnen OCR-Durchläufe.
Die zusätzliche Bildaufbereitung verwendet nur temporäre Dateien, die direkt
danach gelöscht werden. Das gespeicherte Foto für die Prüfung bleibt unverändert.
Alle Tesseract-Durchläufe teilen sich ein Zeitbudget von 25 Sekunden.

Für offene Fotos gibt es auf der Prüfseite **„Texterkennung erneut starten“**,
etwa nach einem OCR-Update. Dadurch wird nur der Erkennungsvorschlag erneuert;
Monat und Verbrauchsbuchungen ändern sich nicht. Ein identischer erneuter
API-Upload liefert dagegen weiterhin den bestehenden Entwurf (Idempotenz).

### Fehlgeschlagene Uploads

Unter **„Fotos prüfen“ → „Fehlgeschlagene Uploads“** erscheinen abgewiesene
Foto-Uploads aus dem Kurzbefehl und dem Webformular: Zeitpunkt (UTC), HTTP-Status,
Fehlergrund, Verarbeitungsschritt, Anfrageformat und -größe sowie – falls bereits
ausgelesen – die Namen der Datei- und Textfelder. Feldwerte, Zugangsdaten und
zusätzliche Bilddateien werden dafür nicht gespeichert. Das Protokoll behält
automatisch nur die letzten 100 Fehler und ist Teil des SQLite-Backups.

Auch Fehler vor der eigentlichen Fotoverarbeitung werden erfasst, z. B. fehlende
Anmeldung (401), fehlendes Dateifeld oder ungültiges Bild (400), Konflikte (409)
und zu große Anfragen (413). Bei internen Fehlern (500) nennt die Übersicht den
Verarbeitungsschritt; technische Details stehen im Serverlog. Dort erscheint
auch für jeden fehlgeschlagenen Upload eine Warnung mit Fehlergrund und
Versuchsnummer (`X-Upload-Failure-ID` im Response-Header).

Die Datenbank wird beim App-Start automatisch erweitert. Frühere Fehlversuche
können nicht nachträglich rekonstruiert werden. Anfragen, die bereits Traefik
oder Gunicorn abweisen und die Flask nicht erreichen, können hier nicht erfasst
werden. Ist die Datenbank nicht beschreibbar, bleibt der Fehler im Serverlog.

### Aufbewahrung der Fotos

- **Noch nicht geprüft:** Die Bilddatei bleibt für die Prüfung erhalten.
- **Erfolgreich übernommen oder ausdrücklich verworfen:** Die Bilddatei wird
  unmittelbar nach dem erfolgreichen Datenbank-Commit gelöscht.
- **Fehler bei der Übernahme**, etwa ein schon vorhandener Monatswert: Das Foto
  bleibt erhalten, damit du die Eingabe korrigieren kannst.

Messwert, Monat, OCR-Text und Upload-ID bleiben als kleine Datenbankeinträge
erhalten. Auch nach dem Löschen verhindert der Bild-Hash doppelte Uploads.
Die Detailseite zeigt bei abgeschlossenen Vorgängen kein Bild mehr an.
Ein später gelöschter Verbrauchseintrag erzeugt keinen neuen Fotoentwurf;
der Wert kann bei Bedarf manuell neu erfasst werden.

Beim App-Start werden auch noch vorhandene Bilder bereits abgeschlossener
Vorgänge aus älteren Versionen bereinigt. Falls die Dateilöschung scheitert,
bleibt die Buchung erhalten; die Oberfläche weist darauf hin und beim nächsten
Start wird die Löschung erneut versucht. Offene Entwürfe werden nicht nach
einer festen Frist gelöscht. Nicht mehr benötigte Aufnahmen bitte verwerfen.
Bereits erstellte Backups werden nicht nachträglich verändert.

Ein identischer Upload mit demselben Monat liefert das bestehende Ergebnis
mit `200 OK`. Ein wiederverwendeter Schlüssel mit anderem Bild oder Monat
liefert `409 Conflict`. Auch ein Foto, das bereits für einen anderen Monat
vorliegt, wird nicht erneut angelegt. Gleichzeitig eintreffende Duplikate können
mit `409` antworten; ein anschließender erneuter identischer Request liefert
das vorhandene Ergebnis. Es wird dabei nie doppelt gebucht.

Statusabfrage: `GET /api/uploads/<id>` mit demselben Bearer-Token.
Der API-Token erlaubt keine Buchungen, Änderungen oder Löschungen von Verbrauch.

### Ablauf im iPhone-Kurzbefehl

Die ausführliche Anleitung findest du in der Anwendung unter **„Fotos prüfen“ →
„iPhone-Kurzbefehl einrichten“**, auch vom Foto-Upload aus erreichbar. Sie enthält
kopierbare Adressen der geöffneten Installation einschließlich Subpath.
Den Upload-Token entnimmst du der Server-`.env`; auf der Anleitungsseite wird er nicht angezeigt.

1. Foto aufnehmen oder auswählen.
2. Bild in JPEG konvertieren, ggf. verkleinern.
3. Verbrauchsmonat bestimmen und zur Kontrolle anzeigen.
4. Mit „Inhalte von URL abrufen“ als POST und Formular senden: `image` = Bild,
   `month` = Monat. Header `Authorization` = `Bearer <UPLOAD_TOKEN>`.
5. `review_url` aus der JSON-Antwort mit Schema und Host der App kombinieren und
   im Browser öffnen. Dort anmelden und den Messwert bestätigen.

Bei langsamer Verbindung einen Timeout oder `409` nicht als erfolgreiche
Buchung interpretieren; die Liste „Fotos prüfen“ zeigt eingegangene Aufnahmen.

## Backup und Wiederherstellung

Das Volume ist Persistenz, kein Backup. Ein laufendes SQLite-WAL-Databasefile
nicht isoliert kopieren. Der Backup-Befehl nutzt die SQLite-Backup-API und
kopiert nur die Fotos der noch offenen Prüfungen im Snapshot. Eine gemeinsame
Dateisperre verhindert, dass eine gleichzeitige Bestätigung während der
Sicherung ein benötigtes Foto löscht. Abgeschlossene Vorgänge bleiben ohne
Bilddatei im Datenbank-Snapshot enthalten.

Beispiel für einen neuen Backup-Namen (bei jeder Sicherung ändern):

```sh
docker compose exec gasverbrauch flask --app gas backup /tmp/gas-backup-2026-09-09
mkdir -p backups
docker compose cp gasverbrauch:/tmp/gas-backup-2026-09-09 backups/
```

Erst die Erfolgsmeldung abwarten, dann den vollständigen Ordner auf einen
anderen Datenträger/Host sichern. `/tmp` ist flüchtig und im Beispiel auf
128 MiB begrenzt; bei größerer Fotosammlung für Backups ein zusätzliches
beschreibbares Backup-Verzeichnis einbinden. `.env` separat sicher verwahren.

Wiederherstellung: App stoppen, den bisherigen Datenbestand aufbewahren,
`gas.sqlite3` und den vollständigen Ordner `photos/` aus dem Backup in ein
**leeres** Datenvolume kopieren, Eigentümer auf `10001:10001` setzen und die
App mit diesem Volume starten. Keine alten `-wal`-/`-shm`-Dateien mit einem
Snapshot mischen. Das Backup enthält auch Konfiguration und Änderungsprotokoll.

## Lokale Entwicklung und Tests

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py init-env
.venv/bin/python manage.py import-ods Gasverbrauch.ods
.venv/bin/python manage.py import-ods Gasverbrauch.ods --apply
.venv/bin/python manage.py serve
```

Eine bereits angelegte `.env` weiterverwenden und den `init-env`-Schritt
überspringen. `serve` bindet Gunicorn ausschließlich an Loopback, Standardport
8080. Lokale Daten liegen unter `data/`. Tesseract muss für lokale OCR zusätzlich
installiert sein; im Container ist es enthalten. Ohne Tesseract funktioniert die
manuelle Fotoauswertung weiterhin.

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python manage.py backup backups/test-restore
```

Die Tests verwenden temporäre Datenbanken und ändern die ODS-Datei nicht. Sie
prüfen den echten ODS-Import, Null/fehlend, Korrekturen, Prognosen, Preise,
Authentifizierung, CSRF, Idempotenz, Upload-Prüfung und Backup/Restore. Der
Tesseract-Test verwendet ein synthetisches Bild; die Erkennungsqualität des
konkreten Thermendisplays muss noch mit echten Fotos erprobt werden.

## Struktur

```text
gas/
  __init__.py   Flask-Routen, Authentifizierung und CLI
  db.py         SQLite-Schema und Änderungsprotokoll
  domain.py     Bestands-, Kosten- und Prognoserechnung
  importer.py   Einmaliger ODS-Import
  ocr.py        Bildprüfung und Tesseract
  templates/    HTML-Oberfläche
  static/       Lokales CSS
tests/          Integrations- und Rechentests
```
