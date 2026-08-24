# Gipfelaufstiege aus dem OSM-Fußnetz

Der Workflow **Gipfelaufstieg erzeugen** wird ausschließlich manuell gestartet.
Er lädt den aktuellen Geofabrik-Extrakt eines Landes und erzeugt genau einen
GeoJSONSeq-Datensatz für den angegebenen Gipfel.

Ein Datensatz wird nur geschrieben, wenn alle Bedingungen erfüllt sind:

- ein `natural=peak` mit exakt passendem Namen existiert,
- ein kartierter Startpunkt wie Parkplatz oder Wanderinformation vorhanden ist,
- ein begehbares OSM-Wegenetz den Startpunkt mit dem Gipfel verbindet,
- die finale Position höchstens 100 m vom Gipfel liegt,
- keine private oder ausdrücklich gesperrte Verbindung genutzt wird.

`climbing:grade:uiaa`, `sac_scale` und `via_ferrata_scale` der verwendeten
Wege werden mitgegeben. Ein UIAA- oder Klettersteigwert ergänzt die passenden
Sicherheitswarnungen. Der Workflow schreibt keine Rundtouren.

## In die Hauptanwendung übernehmen

1. Artefakt `summit-ascent-…` herunterladen und entpacken.
2. Die `.geojsonseq` auf den App-Server außerhalb des Webroots legen.
3. Erst prüfen, dann importieren:

```sh
sudo -u www-data php scripts/import-summit-routes.php \
  --input=/srv/import/summit-ascents-AT.geojsonseq \
  --database=/var/lib/bergchronik/tourdata/bergchronik-summit-routes.sqlite \
  --dry-run

sudo -u www-data php scripts/import-summit-routes.php \
  --input=/srv/import/summit-ascents-AT.geojsonseq \
  --database=/var/lib/bergchronik/tourdata/bergchronik-summit-routes.sqlite
```

Ein fehlendes Ergebnis ist absichtlich kein Ersatz durch eine erfundene Linie:
Entweder endet das OSM-Netz nicht am Gipfel oder ein geeigneter Startpunkt ist
nicht kartiert. Solche Ziele bleiben für eine fachlich geprüfte Ergänzung offen.
