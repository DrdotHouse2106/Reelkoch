# Reelkoch – Rezepte aus Reels

Link zu einem Koch-Reel einfügen, fertiges Rezept als PDF bekommen.

Ablauf: **yt-dlp** lädt Video und Beschreibung → **faster-whisper** schreibt den Ton mit (lokal, CPU) → **ffmpeg** zieht 1 Bild pro Sekunde, fast gleiche Bilder fallen weg, höchstens 15 bleiben → **Claude** liest Beschreibung, Transkript und Bilder und gibt das Rezept als JSON zurück (immer auf Deutsch, nichts erfunden) → **WeasyPrint** erzeugt das PDF mit Vorschaubild. Video, Ton und Bilder werden danach gelöscht, nur PDF und JSON bleiben.

Erlaubt sind Links von Facebook, Instagram und TikTok (`ERLAUBTE_HOSTS`). Instagram und TikTok laufen über denselben yt-dlp-Weg und sind ungetestet.

## Aufbau

| Datei | Inhalt |
|---|---|
| `app/main.py` | FastAPI: Routen, Warteschlange |
| `app/konten.py` | Benutzer, Sitzungen, Zwei-Faktor (TOTP), API-Tokens |
| `app/verarbeitung.py` | Download, Transkript, Standbilder, Claude, PDF, Paperless |
| `app/index.html` | Oberfläche: Anmeldung, Rezepte, Konto, Benutzerverwaltung |
| `deploy/` | Einrichtung im LXC: `install.sh`, systemd-Dienst, yt-dlp-Update-Timer, Beispiel-`.env` |

Daten liegen in `/var/lib/rezepte`: `rezepte/` (je Rezept `<id>.json` und `<id>.pdf`), `benutzer.json` und `sitzungen.json` (nur Hashes, chmod 600), dazu `tmp/` (Arbeitsordner) und `modelle/` (Whisper-Modell). Bewusst nicht `/tmp`, denn das liegt im LXC im Arbeitsspeicher.

Aufträge laufen nacheinander in einem Hintergrund-Thread. Nach einem Neustart werden unfertige Aufträge neu eingereiht.

## Zugang

- **Anmeldung** mit Benutzername, Passwort und **Zwei-Faktor-Code** (TOTP, jede Authenticator-App). 2FA ist für alle Konten Pflicht: Ohne sie sieht man nach der Anmeldung nur die Einrichtung per QR-Code.
- **Ersteinrichtung:** Solange es kein Konto gibt, zeigt die Startseite „Reelkoch einrichten“. Dafür braucht man den Einrichtungscode aus `/var/lib/rezepte/einrichtungscode` (steht auch in `journalctl -u rezepte`). So kann niemand aus dem Internet das erste Konto übernehmen. Nach der Einrichtung wird der Code gelöscht.
- **Benutzer:** Admins legen unter „Benutzer“ weitere Konten an, setzen Passwort und 2FA zurück oder löschen Konten. Alle sehen dieselbe Rezeptliste, löschen dürfen sie nur eigene Rezepte (Admins alle).
- **Kurzbefehl/API:** Jeder erzeugt unter „Konto“ sein eigenes Token, Kopfzeile `Authorization: Bearer <Token>`. Das Token gilt ohne 2FA, also wie ein Passwort behandeln. Gespeichert wird nur der Hash.
- **Schutz:** Passwörter mit scrypt, Sitzungs-Cookie `HttpOnly`, `Secure`, `SameSite=Strict`, 30 Tage gültig. Nach 5 Fehlversuchen in 15 Minuten wird die IP gesperrt. Ändert man das Passwort oder setzt ein Admin ein Konto zurück, werden alle Sitzungen abgemeldet. Die API-Doku von FastAPI ist abgeschaltet.

## API

| Aufruf | Zweck |
|---|---|
| `POST /api/rezept` mit `{"url": "…"}` | Auftrag anlegen, Antwort `{"id", "status"}` |
| `GET /api/rezept/<id>` | Status (`wartet`, Zwischenschritte, `fertig`, `Fehler`) und Rezept-JSON |
| `GET /rezept/<id>.pdf` | PDF, sobald `fertig` |
| `GET /api/rezepte` | Liste aller Rezepte |
| `DELETE /api/rezept/<id>` | Rezept löschen |

## Einrichtung

### 1. Container oder VM

Debian 13 (z. B. als LXC unter Proxmox), 4 Kerne, 3 GB RAM, 12 GB Platte. Whisper „small“ braucht etwa 1 GB RAM, ein 60-Sekunden-Reel dauert auf der CPU rund 1–2 Minuten.

### 2. App installieren (als root)

```bash
apt-get update && apt-get install -y git
git clone https://github.com/DrdotHouse2106/reelkoch /opt/rezepte
sh /opt/rezepte/deploy/install.sh
```

Dann in `/etc/rezepte.env` den Claude-API-Key und `FORWARDED_ALLOW_IPS` (IP des Reverse-Proxys) eintragen und `systemctl restart rezepte`. Danach im Browser das Admin-Konto anlegen (siehe „Zugang“). Ohne HTTPS (Test im lokalen Netz unter `http://<IP>:8000`) braucht es vorübergehend `COOKIE_SECURE=0`, sonst speichert der Browser die Anmeldung nicht.

**Updates:** `sh /opt/rezepte/deploy/install.sh` erneut ausführen. yt-dlp aktualisiert sich jeden Morgen selbst (`yt-dlp-update.timer`). Weil yt-dlp als eigener Prozess läuft, braucht es dafür keinen Neustart.

### 3. Von außen erreichbar machen

Die App spricht nur HTTP. Davor gehört ein Reverse-Proxy mit HTTPS (z. B. nginx mit Let's Encrypt), erreichbar etwa über VPN oder Tunnel zum Server. Beispiel für nginx:

```nginx
location / {
    proxy_pass http://<IP-der-App>:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Nur über HTTPS freigeben, sonst gehen Passwort und Token im Klartext durchs Netz. `FORWARDED_ALLOW_IPS` muss die Adresse sein, von der der Proxy die App erreicht. Sonst sieht die Sperre nach Fehlversuchen nur die Proxy-IP und sperrt im Zweifel alle.

## Häufige Probleme

- **`ModuleNotFoundError: No module named 'httpx'`** beim Start: `anthropic` ab 2.8 bringt nicht mehr `httpx` mit, sondern `httpx2`. Die App importiert `httpx` direkt, deshalb steht `httpx` jetzt ausdrücklich in `requirements.txt`. Bei älteren Installationen hilft `/opt/rezepte/.venv/bin/pip install httpx && systemctl restart rezepte`.
- **`detected dubious ownership`** nach dem Kopieren des Repos in den LXC: `chown -R root:root /opt/rezepte`.
- **Ohne `ANTHROPIC_API_KEY`** startet `install.sh` den Dienst nicht neu. Der Dienst läuft trotzdem und antwortet ohne Anmeldung mit 401. Rezepte gehen erst mit Schlüssel.
- **Hinweis zu `HF_TOKEN` im Log** beim ersten Start: Das Whisper-Modell wird ohne Hugging-Face-Konto geladen. Der Hinweis ist harmlos.
- **Nach der Anmeldung wieder die Anmeldeseite:** Die Seite läuft ohne HTTPS, der Browser verwirft das `Secure`-Cookie. HTTPS nutzen oder zum Testen `COOKIE_SECURE=0` setzen.
- **2FA-Code wird abgelehnt:** Die Uhr von Server oder Handy geht falsch (Toleranz ±30 Sekunden). Jeder Code gilt nur einmal.
- **Admin hat 2FA-Gerät verloren:** Ein anderer Admin setzt das Konto zurück. Gibt es keinen, im Container den Eintrag `"totp"` des Kontos in `/var/lib/rezepte/benutzer.json` auf `null` setzen.
- **Update von der Version mit Basic Auth:** `WEB_BENUTZER`, `WEB_PASSWORT` und `API_TOKEN` in `/etc/rezepte.env` werden nicht mehr gebraucht und können raus. Vorhandene Rezepte zieht die App beim Start automatisch nach `rezepte/` um.
- **`/tmp` liegt im LXC im RAM**: Die App nutzt deshalb `/var/lib/rezepte/tmp` für Downloads und Tonspuren.
- **Langsame Transkription** auf schwacher CPU: Whisper „small“ auf 2 Kernen einer älteren Mobil-CPU braucht etwa so lange wie das Reel selbst. Eine Intel-Grafik hilft nicht, `faster-whisper` beschleunigt nur mit NVIDIA (CUDA).

## iPhone-Kurzbefehl „Rezept erstellen“

1. Kurzbefehl anlegen, in den Details **„Im Share-Sheet anzeigen“** einschalten, Typ **URLs**.
2. Aktion **„Inhalte von URL abrufen“**: `https://<deine-domain>/api/rezept`, Methode **POST**, Header `Authorization` = `Bearer <Token>` (Token unter „Konto“ erzeugen), Haupttext **JSON** mit Feld `url` = **Kurzbefehleingabe**.
3. **„Wörterbuchwert abrufen“** für `id`, als Variable `id` sichern.
4. **„Wiederholen“** 40-mal:
   - **„Warten“** 10 Sekunden
   - **„Inhalte von URL abrufen“** `https://<deine-domain>/api/rezept/[id]` (GET, gleicher Header)
   - **„Wörterbuchwert abrufen“** `status`
   - **„Wenn“** `status` ist `fertig`: **„Inhalte von URL abrufen“** `https://<deine-domain>/rezept/[id].pdf` (gleicher Header) → **„Schnellansicht“** → **„Kurzbefehl stoppen“**
   - **„Wenn“** `status` ist `Fehler`: **„Mitteilung zeigen“** mit `fehler` → **„Kurzbefehl stoppen“**
5. Danach eine **„Mitteilung zeigen“**: „Dauert noch, das PDF erscheint auf der Webseite“.

Im Reel dann **Teilen → Link kopieren** bzw. **Teilen → Rezept erstellen**.

## Paperless (optional)

`PAPERLESS_URL`, `PAPERLESS_TOKEN` und `PAPERLESS_TAG_ID` (ID des Tags „Rezept“) in `/etc/rezepte.env` setzen. Jedes fertige PDF wird dann hochgeladen. Schlägt das fehl, bleibt das Rezept trotzdem fertig, der Fehler steht im JSON unter `paperless_fehler`.

## Kosten

Jedes Rezept speichert Token-Verbrauch und geschätzte Kosten (`verbrauch` im JSON, Zeile im Log). Die Liste zeigt die Kosten in US-Dollar an.

Ein Reel mit 15 Bildern braucht bei Claude Opus 5.5 rund 20.000 Eingabe-Token (Hochformat-Bilder), also etwa 8–10 Cent. Mit `CLAUDE_MODELL=claude-sonnet-5-5` ist es etwa die Hälfte. Lehnt das Modell eine Anfrage ab, springt die serverseitige Ausweichlösung (`fallbacks: "default"`) ein.

## Lizenz

[MIT](LICENSE) – frei nutzbar, auch kommerziell, ohne Gewähr.
