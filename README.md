# BergChronik TourData

Dieses Repository erzeugt einen durchsuchbaren Katalog der in OpenStreetMap
erfassten Wander- und Fußrouten für Österreich, Deutschland, die Schweiz und
Italien. Die komplette Verarbeitung läuft in GitHub Actions. Auf dem eigenen
Computer muss nichts installiert werden.

## Was "alle Wandertouren" bedeutet

Der Export enthält alle passenden Relationen im jeweiligen täglichen
Geofabrik-Länderextrakt:

- `type=route`
- `route=hiking` oder `route=foot`
- `network=lwn`, `rwn`, `nwn` und `iwn` werden gespeichert, sind aber kein
  Pflichtfilter

Damit sind alle zu diesem Zeitpunkt in OpenStreetMap erfassten passenden
Routen gemeint. Nicht in OpenStreetMap eingetragene Touren können technisch
nicht enthalten sein. Private Nutzeraufzeichnungen und eigene
BergChronik-Touren sind ausdrücklich nicht Teil dieses Katalogs.

## Ergebnisse

Jeder Länderjob erzeugt ein ZIP-Artefakt:

| Datei | Zweck |
| --- | --- |
| `bergchronik-routes-AT.sqlite` | kompakte Suchdatenbank mit FTS5 und RTree |
| `bergchronik-routes-AT.geojson` | vollständige Linien für Karten und GIS |
| `manifest-AT.json` | Quelle, Datenstand, Umfang und Dateiinformationen |
| `bergchronik-routes-AT-gpx.zip` | optional eine GPX-Datei pro OSM-Relation |

Die Dateinamen verwenden entsprechend `DE`, `CH` oder `IT`. Für BergChronik
ist SQLite die Hauptquelle. GPX wird aus derselben Geometrie erzeugt und muss
nicht einzeln im Git verwaltet werden.

## Repository einmalig erstellen

Das fertige Projekt liegt unter:

https://github.com/Fxbso/BergChronik-TourData

Wer es in einem anderen GitHub-Konto neu anlegen möchte:

1. Auf GitHub **New repository** wählen.
2. Den Namen `BergChronik-TourData` vergeben.
3. Ein leeres öffentliches Repository ohne automatisch erzeugte Dateien
   erstellen.
4. Den vollständigen Inhalt dieses Projekts in den `main`-Branch übernehmen.
5. Unter **Actions** prüfen, dass Workflows für das Repository erlaubt sind.

Es werden keine Secrets und keine externen Zugangsdaten benötigt.

## Workflow starten

1. Das Repository auf GitHub öffnen.
2. **Actions** auswählen.
3. Links **Wanderrouten exportieren** öffnen.
4. **Run workflow** wählen.
5. Bei **Zu verarbeitendes Land** entweder `ALL`, `AT`, `DE`, `CH` oder `IT`
   auswählen.
6. **Zusätzlich eine GPX-Datei pro Route erzeugen** nur aktivieren, wenn das
   große GPX-Archiv tatsächlich gebraucht wird.
7. Mit **Run workflow** starten.

`ALL` startet vier Matrixjobs parallel. Schlägt nur ein Land fehl, kann der
Workflow über **Re-run failed jobs** erneut ausgeführt oder gezielt nur dieses
Land neu gestartet werden. Jeder Job hat eigene Rohdaten und ein eigenes
Artefakt.

## Artefakte herunterladen

1. Nach Abschluss den Workflow-Lauf öffnen.
2. Am Ende der Seite den Abschnitt **Artifacts** öffnen.
3. `bergchronik-routes-AT`, `bergchronik-routes-DE`,
   `bergchronik-routes-CH` oder `bergchronik-routes-IT` herunterladen.
4. Das heruntergeladene Actions-Archiv entpacken. Darin befindet sich das
   eigentliche Länder-ZIP mit Datenbank und Exporten.

Die Actions-Artefakte bleiben 14 Tage verfügbar. Für eine dauerhafte Ablage
sollten geprüfte Länder-ZIPs anschließend als GitHub-Release veröffentlicht
oder in den vorgesehenen Objektspeicher kopiert werden. Die großen
Datenprodukte werden absichtlich nicht in Git committed.

## Verarbeitung

Der Workflow verwendet ausschließlich bereits auf `ubuntu-latest`
vorhandene Werkzeuge:

- Python 3
- SQLite
- `curl`
- `zip` und `unzip`

Der enthaltene Streaming-Parser liest die für Wanderrouten benötigten
OSM-PBF-Strukturen ohne `pip`, `apt`, Docker oder ein selbst gebautes Image.
Die Datei wird in drei sequenziellen Durchläufen verarbeitet:

1. passende Relationen und ihre geordnete Mitgliederliste lesen
2. nur die von diesen Relationen benötigten Ways lesen
3. nur die von diesen Ways benötigten Nodes lesen

Dadurch wird kein vollständiger Länderextrakt in den Arbeitsspeicher geladen.
Verschachtelte Routenrelationen, Rollen wie `backward` und getrennte
Liniensegmente werden berücksichtigt.

## Datenbank

Die Tabelle `routes` enthält unter anderem:

- OSM-Relations-ID und Land
- Name, Referenz, Betreiber und Netzwerk
- Wander- oder Fußroute
- berechnete Distanz
- in OSM vorhandene Angaben zu Aufstieg, Abstieg und Dauer
- Kartenmittelpunkt und Bounding Box
- komprimierte MultiLineString-Geometrie
- vollständige OSM-Tags
- Qualitätskennzeichen
- Quelldatum und OSM-Link

`route_search` ist ein FTS5-Volltextindex. `route_bounds` ist ein RTree-Index
für Kartenausschnitte.

Beispiel für die Suche in PHP:

```php
$db = new PDO('sqlite:/path/bergchronik-routes-AT.sqlite');
$sql = <<<'SQL'
SELECT r.osm_relation_id, r.name, r.route_type, r.network,
       r.distance_m, r.ascent_m, r.center_lat, r.center_lon
FROM route_search s
JOIN routes r ON r.id = s.rowid
WHERE route_search MATCH :query
ORDER BY bm25(route_search), r.name
LIMIT 30
SQL;
$statement = $db->prepare($sql);
$statement->execute(['query' => $searchTerm]);
$routes = $statement->fetchAll(PDO::FETCH_ASSOC);
```

Eigene Touren bleiben in der bisherigen BergChronik-Tabelle. Die öffentliche
Suche muss ausschließlich diese Katalogdatenbanken abfragen; dadurch werden
Nutzeraufzeichnungen nicht als allgemeine Suchergebnisse angezeigt.

## Qualitätskennzeichen

`quality_flags` kann folgende Werte enthalten:

- `disconnected`: Die OSM-Relation besteht aus getrennten Segmenten.
- `missing_way`: Ein referenzierter Way war im Extrakt nicht verfügbar.
- `missing_node`: Mindestens ein referenzierter Punkt fehlte.
- `unnamed`: Die Relation hat weder Namen noch deutschsprachigen Namen.
- `proposed`, `abandoned`, `disused`: entsprechender OSM-Status.
- `nonstandard_network`: Netzwerk außerhalb `lwn`, `rwn`, `nwn`, `iwn`.

Diese Routen bleiben auffindbar, können in BergChronik aber sichtbar
gekennzeichnet oder aus Standardergebnissen ausgeblendet werden.

## Lokal prüfen, ohne etwas zu installieren

Nur wenn Python bereits vorhanden ist:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Dieser Schritt ist optional. Die gleiche Prüfung läuft automatisch in GitHub
Actions.

## Quellen und Lizenz

Die Rohdaten kommen aus den täglich aktualisierten Länderextrakten von
[Geofabrik](https://download.geofabrik.de/europe/). Die erzeugten Daten stehen
unter der Open Database License 1.0:

**© OpenStreetMap-Mitwirkende**

Details stehen in [LICENSE-DATA.md](LICENSE-DATA.md). Der Programmcode steht
unter der MIT-Lizenz.

