"""Verarbeitung eines Reels: Download, Transkript, Standbilder, Claude, PDF."""
import base64, html, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

import anthropic
import numpy as np
import httpx
from PIL import Image
from weasyprint import HTML

DATEN = Path(os.environ.get("DATEN_DIR", "/var/lib/rezepte"))
MODELL = os.environ.get("CLAUDE_MODELL", "claude-opus-5-5")
WHISPER_MODELL = os.environ.get("WHISPER_MODELL", "small")
MAX_BILDER = int(os.environ.get("MAX_BILDER", "15"))
# US-Dollar je 1 Mio. Token (Eingabe, Ausgabe), nur für die Kostenanzeige
PREISE = {"claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10), "claude-opus-4-8": (5, 25), "claude-sonnet-5": (2, 10)}

_whisper = None  # wird beim ersten Auftrag geladen (spart Speicher beim Start)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["titel", "portionen", "zeit", "zutaten", "schritte", "hinweise", "bild_index"],
    "properties": {
        "titel": {"type": "string"},
        "portionen": {"type": ["string", "null"]},
        "zeit": {"type": ["string", "null"]},
        "zutaten": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["menge", "zutat"],
            "properties": {"menge": {"type": "string"}, "zutat": {"type": "string"}}}},
        "schritte": {"type": "array", "items": {"type": "string"}},
        "hinweise": {"type": ["string", "null"]},
        "bild_index": {"type": "integer"},
    },
}

ANWEISUNG = """Du bekommst Beschreibung, Transkript und Standbilder eines Koch-Reels.
Erstelle daraus das Rezept als JSON.
- Schreibe alles auf Deutsch, auch wenn das Reel eine andere Sprache hat.
- Erfinde nichts. Fehlende Mengen übernimmst du so, wie sie gesagt oder gezeigt werden
  (z. B. „etwas“, „1 Schuss“). Ist gar keine Menge erkennbar, lass „menge“ leer.
- Mengen aus Text-Einblendungen in den Bildern zählen genauso wie gesprochene.
- portionen und zeit nur, wenn sie genannt werden, sonst null.
- hinweise: kurze Tipps aus dem Reel, sonst null. Ist gar kein Rezept erkennbar, sag das hier.
- bild_index: Nummer (ab 0) des Bildes, das das fertige Gericht am besten zeigt."""


def _ytdlp(url: str, ordner: Path) -> dict:
    # yt-dlp als eigener Prozess: Updates wirken ohne Neustart der App
    subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--no-playlist", "--write-info-json",
         "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
         "-o", str(ordner / "video.%(ext)s"), url],
        check=True, capture_output=True, text=True, timeout=600)
    return json.loads((ordner / "video.info.json").read_text())


def _transkript(video: Path) -> str:
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel(WHISPER_MODELL, device="cpu", compute_type="int8")
    # Ton per ffmpeg als 16-kHz-Mono lesen (unabhängig von PyAV-Versionen)
    ton = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, timeout=600).stdout
    if not ton:  # Video ohne Tonspur
        return ""
    segmente, _ = _whisper.transcribe(np.frombuffer(ton, np.int16).astype(np.float32) / 32768, vad_filter=True)
    return " ".join(s.text.strip() for s in segmente)


def _hash(bild: Path) -> int:
    pixel = list(Image.open(bild).convert("L").resize((8, 8)).getdata())
    mittel = sum(pixel) / 64
    return sum(1 << i for i, p in enumerate(pixel) if p > mittel)


def _standbilder(video: Path, ordner: Path) -> list[Path]:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf", "fps=1,scale=768:-2",
                    "-q:v", "4", str(ordner / "bild_%04d.jpg")], check=True, timeout=600)
    behalten, letzter = [], None
    for bild in sorted(ordner.glob("bild_*.jpg")):
        h = _hash(bild)
        if letzter is None or bin(h ^ letzter).count("1") > 10:  # fast gleiche Bilder verwerfen
            behalten.append(bild)
            letzter = h
    if len(behalten) > MAX_BILDER:  # gleichmäßig ausdünnen
        schritt = len(behalten) / MAX_BILDER
        behalten = [behalten[int(i * schritt)] for i in range(MAX_BILDER)]
    return behalten


def _b64(bild: Path) -> str:
    return base64.standard_b64encode(bild.read_bytes()).decode()


def _claude(beschreibung: str, transkript: str, bilder: list[Path]) -> dict:
    inhalt = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(b)}}
              for b in bilder]
    inhalt.append({"type": "text", "text": f"{ANWEISUNG}\n\nBeschreibung:\n{beschreibung or '(keine)'}"
                                           f"\n\nTranskript:\n{transkript or '(kein Ton)'}"})
    antwort = anthropic.Anthropic().beta.messages.create(
        model=MODELL, max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": inhalt}])
    if antwort.stop_reason == "refusal":
        raise RuntimeError("Claude hat die Anfrage abgelehnt.")
    text = next(b.text for b in antwort.content if b.type == "text")
    rezept = json.loads(text)
    u = antwort.usage
    ein = u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
    preis = PREISE.get(antwort.model)
    rezept["verbrauch"] = {"modell": antwort.model, "eingabe": ein, "ausgabe": u.output_tokens,
                           "kosten_usd": preis and round((ein * preis[0] + u.output_tokens * preis[1]) / 1e6, 4)}
    return rezept


def _pdf(rezept: dict, bild: Path | None, ziel: Path):
    e = lambda s: html.escape(s or "")
    zutaten = "".join(f"<tr><td>{e(z['menge'])}</td><td>{e(z['zutat'])}</td></tr>" for z in rezept["zutaten"])
    schritte = "".join(f"<li>{e(s)}</li>" for s in rezept["schritte"])
    info = " · ".join(x for x in [rezept["portionen"] and f"Portionen: {e(rezept['portionen'])}",
                                  rezept["zeit"] and f"Zeit: {e(rezept['zeit'])}"] if x)
    bild_html = f'<img src="data:image/jpeg;base64,{_b64(bild)}">' if bild else ""
    hinweise = f"<h2>Hinweise</h2><p>{e(rezept['hinweise'])}</p>" if rezept.get("hinweise") else ""
    HTML(string=f"""<!doctype html><html lang="de"><meta charset="utf-8"><style>
@page {{ size: A4; margin: 18mm; }}
body {{ font-family: "DejaVu Sans", sans-serif; font-size: 11pt; line-height: 1.45; color: #111; }}
h1 {{ font-size: 20pt; margin: 0 0 4pt; }} h2 {{ font-size: 13pt; margin: 14pt 0 6pt; }}
.info {{ color: #555; }} img {{ float: right; width: 55mm; height: 75mm; object-fit: cover; margin: 0 0 8pt 10pt; border-radius: 3mm; }}
table {{ border-collapse: collapse; }} td {{ padding: 2pt 8pt 2pt 0; vertical-align: top; }}
td:first-child {{ white-space: nowrap; font-weight: bold; }} li {{ margin-bottom: 5pt; }}
.quelle {{ clear: both; margin-top: 16pt; font-size: 8.5pt; color: #777; word-break: break-all; }}
</style><body>{bild_html}<h1>{e(rezept['titel'])}</h1><p class="info">{info}</p>
<h2>Zutaten</h2><table>{zutaten}</table><h2>Zubereitung</h2><ol>{schritte}</ol>{hinweise}
<p class="quelle">Quelle: {e(rezept['quelle'])}</p></body></html>""").write_pdf(ziel)


def _paperless(pdf: Path, titel: str):
    """Optional: PDF an Paperless übergeben, wenn PAPERLESS_URL gesetzt ist."""
    url = os.environ.get("PAPERLESS_URL")
    if not url:
        return
    daten = {"title": titel}
    if os.environ.get("PAPERLESS_TAG_ID"):
        daten["tags"] = os.environ["PAPERLESS_TAG_ID"]
    httpx.post(f"{url.rstrip('/')}/api/documents/post_document/",
               headers={"Authorization": f"Token {os.environ['PAPERLESS_TOKEN']}"},
               data=daten, files={"document": (pdf.name, pdf.read_bytes(), "application/pdf")},
               timeout=60).raise_for_status()


def verarbeiten(auftrag: dict, status, pdf: Path):
    """Führt alle Schritte aus. status(text) meldet den Fortschritt an die Oberfläche."""
    tmp = DATEN / "tmp"  # nicht /tmp: im LXC liegt /tmp im Arbeitsspeicher
    tmp.mkdir(parents=True, exist_ok=True)
    ordner = Path(tempfile.mkdtemp(dir=tmp))
    try:
        status("Video wird geladen")
        info = _ytdlp(auftrag["url"], ordner)
        video = next(p for p in ordner.glob("video.*") if p.suffix != ".json")
        status("Ton wird transkribiert")
        transkript = _transkript(video)
        status("Standbilder werden erzeugt")
        bilder = _standbilder(video, ordner)
        status("Claude liest das Rezept")
        rezept = _claude(info.get("description") or info.get("title", ""), transkript, bilder)
        rezept["quelle"] = auftrag["url"]
        idx = rezept.pop("bild_index", 0)
        status("PDF wird erstellt")
        _pdf(rezept, bilder[idx] if 0 <= idx < len(bilder) else (bilder[0] if bilder else None), pdf)
        try:
            _paperless(pdf, rezept["titel"])
        except Exception as fehler:
            rezept["paperless_fehler"] = str(fehler)
        return rezept
    finally:
        shutil.rmtree(ordner, ignore_errors=True)
