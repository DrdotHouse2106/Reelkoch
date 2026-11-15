"""Reelkoch: Weboberfläche und API."""
import json, os, queue, threading, time, uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import konten as k
from verarbeitung import DATEN, verarbeiten

HOSTS = os.environ.get("ERLAUBTE_HOSTS", "facebook.com,fb.watch,instagram.com,tiktok.com").split(",")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
auftraege: queue.Queue = queue.Queue()
DATEN.mkdir(parents=True, exist_ok=True)


# --- Zugang ---

def halb(request: Request):
    """Angemeldet, 2FA evtl. noch nicht eingerichtet."""
    b, s = k.wer(request)
    if not b:
        raise HTTPException(401, "Bitte anmelden.")
    return b, s


def voll(z=Depends(halb)):
    """Angemeldet mit 2FA (oder API-Token)."""
    if not z[1]["voll"]:
        raise HTTPException(403, "Bitte zuerst die Zwei-Faktor-Anmeldung einrichten.")
    return z[0]


def admin(b=Depends(voll)):
    if not b["admin"]:
        raise HTTPException(403, "Nur für Admins.")
    return b


def _mit_cookie(daten: dict, sid: str | None) -> JSONResponse:
    antwort = JSONResponse(daten)
    if sid:
        antwort.set_cookie("sitzung", sid, max_age=k.SITZUNG_TAGE * 86400, httponly=True,
                           secure=os.environ.get("COOKIE_SECURE", "1") == "1", samesite="strict")
    else:
        antwort.delete_cookie("sitzung")
    return antwort


@app.get("/", response_class=HTMLResponse)
def startseite():
    return (Path(__file__).parent / "index.html").read_text()


@app.get("/api/status")
def status(request: Request):
    b, s = k.wer(request)
    return {"eingerichtet": k.eingerichtet(),
            "benutzer": b and {"name": b["name"], "admin": b["admin"], "voll": s["voll"]}}


@app.post("/api/einrichten")
def einrichten(e: dict, request: Request):
    ip = k.bremse(request)
    code = k.einrichtungscode()
    if not code:
        raise HTTPException(409, "Ist schon eingerichtet.")
    if str(e.get("code", "")).strip() != code:
        k.fehlschlag(ip, "Einrichtungscode falsch.")
    name = k.benutzer_anlegen(str(e.get("name", "")), str(e.get("passwort", "")), admin=True)
    k.einrichtungscode()  # Code löschen
    return _mit_cookie({"ok": True}, k.sitzung_neu(name, False))


@app.post("/api/anmelden")
def anmelden(e: dict, request: Request):
    ip = k.bremse(request)
    sid, ist_voll = k.anmelden(str(e.get("name", "")), str(e.get("passwort", "")), str(e.get("code", "")), ip)
    return _mit_cookie({"voll": ist_voll}, sid)


@app.post("/api/abmelden")
def abmelden(request: Request):
    k.sitzung_aendern(request.cookies.get("sitzung", ""), loeschen=True)
    return _mit_cookie({"ok": True}, None)


@app.post("/api/2fa/start")
def zwei_faktor_start(request: Request, z=Depends(halb)):
    if z[0]["totp"]:
        raise HTTPException(409, "2FA ist schon eingerichtet. Zurücksetzen kann ein Admin.")
    return k.totp_start(request.cookies.get("sitzung", ""), z[0]["name"])


@app.post("/api/2fa/bestaetigen")
def zwei_faktor_ok(e: dict, request: Request, z=Depends(halb)):
    ip = k.bremse(request)
    sid = k.totp_bestaetigen(request.cookies.get("sitzung", ""), z[1], z[0]["name"], str(e.get("code", "")), ip)
    return _mit_cookie({"ok": True}, sid)


@app.post("/api/passwort")
def passwort(e: dict, request: Request, b=Depends(voll)):
    ip = k.bremse(request)
    if not k.pw_pruefen(str(e.get("alt", "")), b["pw"]):
        k.fehlschlag(ip, "Altes Passwort falsch.")
    neu = str(e.get("neu", ""))
    if len(neu) < 10:
        raise HTTPException(400, "Passwort mindestens 10 Zeichen.")
    k.benutzer_aendern(b["name"], pw=k.pw_hash(neu))  # meldet alle Sitzungen ab
    return _mit_cookie({"ok": True}, k.sitzung_neu(b["name"], True))


@app.post("/api/token")
def token(b=Depends(voll)):
    return {"token": k.token_neu(b["name"])}


# --- Benutzerverwaltung (nur Admins) ---

@app.get("/api/benutzer", dependencies=[Depends(admin)])
def benutzer():
    return k.benutzer_liste()


@app.post("/api/benutzer", dependencies=[Depends(admin)])
def benutzer_neu(e: dict):
    return {"name": k.benutzer_anlegen(str(e.get("name", "")), str(e.get("passwort", "")), bool(e.get("admin")))}


@app.post("/api/benutzer/{name}", dependencies=[Depends(admin)])
def benutzer_zuruecksetzen(name: str, e: dict):
    """Neues Startpasswort, 2FA und Token zurücksetzen."""
    if len(str(e.get("passwort", ""))) < 10:
        raise HTTPException(400, "Passwort mindestens 10 Zeichen.")
    k.benutzer_aendern(name, pw=k.pw_hash(e["passwort"]), totp=None, totp_schritt=0, token=None)
    return {"ok": True}


@app.delete("/api/benutzer/{name}")
def benutzer_loeschen(name: str, b=Depends(admin)):
    if name == b["name"]:
        raise HTTPException(400, "Das eigene Konto kann nicht gelöscht werden.")
    k.benutzer_aendern(name, loeschen=True)
    return {"ok": True}


# --- Rezepte ---

def _pfad(id: str) -> Path:
    if not id.isalnum():
        raise HTTPException(404)
    return DATEN / "rezepte" / f"{id}.json"


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
            daten["rezept"] = verarbeiten(daten, status, DATEN / "rezepte" / f"{daten['id']}.pdf")
            daten["titel"] = daten["rezept"]["titel"]
            daten["kosten_usd"] = daten["rezept"].get("verbrauch", {}).get("kosten_usd")
            print(f"Rezept {daten['id']} fertig, Verbrauch: {daten['rezept'].get('verbrauch')}", flush=True)
            daten["status"] = "fertig"
        except Exception as fehler:
            daten["status"] = "Fehler"
            daten["fehler"] = str(getattr(fehler, "stderr", None) or fehler)[-500:]
        _schreiben(daten)


# Rezepte aus älteren Versionen (direkt im Datenordner) in den Unterordner holen
(DATEN / "rezepte").mkdir(exist_ok=True)
for p in [*DATEN.glob("*.pdf"), *(p for p in DATEN.glob("*.json") if p.stem.isalnum() and len(p.stem) == 12)]:
    p.replace(DATEN / "rezepte" / p.name)
# Liegengebliebene Aufträge (z. B. nach Neustart) wieder einreihen
for p in sorted((DATEN / "rezepte").glob("*.json")):
    if json.loads(p.read_text())["status"] not in ("fertig", "Fehler"):
        auftraege.put(p.stem)
threading.Thread(target=_arbeiter, daemon=True).start()
if code := k.einrichtungscode():
    print(f"Reelkoch ist noch nicht eingerichtet. Einrichtungscode: {code}", flush=True)


@app.post("/api/rezept")
def neu(eingabe: dict, b=Depends(voll)):
    url = str(eingabe.get("url", "")).strip()
    host = urlparse(url).hostname or ""
    if not url.startswith("https://") or not any(host == h or host.endswith("." + h) for h in HOSTS):
        raise HTTPException(400, "Nur Links von " + ", ".join(HOSTS))
    daten = {"id": uuid.uuid4().hex[:12], "url": url, "status": "wartet", "titel": None, "von": b["name"],
             "datum": datetime.now().strftime("%d.%m.%Y %H:%M"), "zeit": time.time()}
    _schreiben(daten)
    auftraege.put(daten["id"])
    return {"id": daten["id"], "status": daten["status"]}


@app.get("/api/rezepte", dependencies=[Depends(voll)])
def liste():
    alle = [json.loads(p.read_text()) for p in (DATEN / "rezepte").glob("*.json")]
    felder = ("id", "url", "status", "titel", "datum", "fehler", "von", "kosten_usd")
    return [{f: d.get(f) for f in felder} for d in sorted(alle, key=lambda d: d.get("zeit", 0), reverse=True)]


@app.get("/api/rezept/{id}", dependencies=[Depends(voll)])
def abfragen(id: str):
    return _lesen(id)


@app.get("/rezept/{id}.pdf", dependencies=[Depends(voll)])
def pdf(id: str):
    daten = _lesen(id)
    if daten["status"] != "fertig":
        raise HTTPException(409, daten["status"])
    name = "".join(c for c in daten["titel"] if c.isalnum() or c in " -_").strip() or id
    return FileResponse(_pfad(id).with_suffix(".pdf"), media_type="application/pdf", filename=f"{name}.pdf",
                        content_disposition_type="inline")  # im Browser anzeigen statt herunterladen


@app.delete("/api/rezept/{id}")
def loeschen(id: str, b=Depends(voll)):
    daten = _lesen(id)
    if not b["admin"] and daten.get("von") != b["name"]:
        raise HTTPException(403, "Nur eigene Rezepte löschen.")
    for endung in (".json", ".pdf"):
        _pfad(id).with_suffix(endung).unlink(missing_ok=True)
    return {"ok": True}
