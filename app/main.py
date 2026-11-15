"""Rezepte aus Reels: Weboberfläche und API."""
import json, os, queue, secrets, threading, time, uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic

from verarbeitung import DATEN, verarbeiten

BENUTZER = os.environ["WEB_BENUTZER"]
PASSWORT = os.environ["WEB_PASSWORT"]
API_TOKEN = os.environ["API_TOKEN"]
HOSTS = os.environ.get("ERLAUBTE_HOSTS", "facebook.com,fb.watch,instagram.com,tiktok.com").split(",")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
basic = HTTPBasic(auto_error=False)
auftraege: queue.Queue = queue.Queue()
DATEN.mkdir(parents=True, exist_ok=True)


def angemeldet(request: Request, cred=Depends(basic)):
    """Zugang per Basic Auth (Browser) oder per Bearer-Token (Kurzbefehl)."""
    kopf = request.headers.get("authorization", "")
    if kopf.startswith("Bearer ") and secrets.compare_digest(kopf[7:], API_TOKEN):
        return
    if cred and secrets.compare_digest(cred.username, BENUTZER) and secrets.compare_digest(cred.password, PASSWORT):
        return
    time.sleep(1)  # bremst Passwortraten
    raise HTTPException(401, "Anmeldung nötig", {"WWW-Authenticate": 'Basic realm="Rezepte"'})


def _pfad(id: str) -> Path:
    if not id.isalnum():
        raise HTTPException(404)
    return DATEN / f"{id}.json"


def _lesen(id: str) -> dict:
    pfad = _pfad(id)
    if not pfad.exists():
        raise HTTPException(404, "Unbekanntes Rezept")
    return json.loads(pfad.read_text())


def _schreiben(daten: dict):
    pfad = _pfad(daten["id"])
    pfad.with_suffix(".tmp").write_text(json.dumps(daten, ensure_ascii=False, indent=1))
    pfad.with_suffix(".tmp").replace(pfad)


def _arbeiter():
    """Ein Auftrag nach dem anderen, damit Whisper die CPU nicht überlastet."""
    while True:
        daten = _lesen(auftraege.get())
        def status(text):
            daten["status"] = text
            _schreiben(daten)
        try:
            daten["rezept"] = verarbeiten(daten, status)
            daten["titel"] = daten["rezept"]["titel"]
            daten["status"] = "fertig"
        except Exception as fehler:
            daten["status"] = "Fehler"
            daten["fehler"] = str(getattr(fehler, "stderr", None) or fehler)[-500:]
        _schreiben(daten)


# Liegengebliebene Aufträge (z. B. nach Neustart) wieder einreihen
for p in sorted(DATEN.glob("*.json")):
    if json.loads(p.read_text())["status"] not in ("fertig", "Fehler"):
        auftraege.put(p.stem)
threading.Thread(target=_arbeiter, daemon=True).start()


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(angemeldet)])
def startseite():
    return (Path(__file__).parent / "index.html").read_text()


@app.post("/api/rezept", dependencies=[Depends(angemeldet)])
def neu(eingabe: dict):
    url = str(eingabe.get("url", "")).strip()
    host = urlparse(url).hostname or ""
    if not url.startswith("https://") or not any(host == h or host.endswith("." + h) for h in HOSTS):
        raise HTTPException(400, "Nur Links von " + ", ".join(HOSTS))
    daten = {"id": uuid.uuid4().hex[:12], "url": url, "status": "wartet", "titel": None,
             "datum": datetime.now().strftime("%d.%m.%Y %H:%M"), "zeit": time.time()}
    _schreiben(daten)
    auftraege.put(daten["id"])
    return {"id": daten["id"], "status": daten["status"]}


@app.get("/api/rezepte", dependencies=[Depends(angemeldet)])
def liste():
    alle = [json.loads(p.read_text()) for p in DATEN.glob("*.json")]
    felder = ("id", "url", "status", "titel", "datum", "fehler")
    return [{k: d.get(k) for k in felder} for d in sorted(alle, key=lambda d: d["zeit"], reverse=True)]


@app.get("/api/rezept/{id}", dependencies=[Depends(angemeldet)])
def abfragen(id: str):
    return _lesen(id)


@app.get("/rezept/{id}.pdf", dependencies=[Depends(angemeldet)])
def pdf(id: str):
    daten = _lesen(id)
    if daten["status"] != "fertig":
        raise HTTPException(409, daten["status"])
    name = "".join(c for c in daten["titel"] if c.isalnum() or c in " -_").strip() or id
    return FileResponse(DATEN / f"{id}.pdf", media_type="application/pdf", filename=f"{name}.pdf",
                        content_disposition_type="inline")  # im Browser anzeigen statt herunterladen


@app.delete("/api/rezept/{id}", dependencies=[Depends(angemeldet)])
def loeschen(id: str):
    _lesen(id)
    for endung in (".json", ".pdf"):
        (DATEN / f"{id}{endung}").unlink(missing_ok=True)
    return {"ok": True}
