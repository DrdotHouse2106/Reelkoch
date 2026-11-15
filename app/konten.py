"""Benutzer, Sitzungen, 2FA (TOTP) und API-Tokens. Gespeichert als JSON im Datenordner."""
import base64, hashlib, hmac, json, secrets, struct, threading, time
from urllib.parse import quote

import segno
from fastapi import HTTPException, Request

from verarbeitung import DATEN

BENUTZER = DATEN / "benutzer.json"
SITZUNGEN = DATEN / "sitzungen.json"
CODE = DATEN / "einrichtungscode"
SITZUNG_TAGE = 30
_sperre = threading.Lock()
_fehlversuche: dict[str, list[float]] = {}


def _laden(pfad) -> dict:
    return json.loads(pfad.read_text()) if pfad.exists() else {}


def _speichern(pfad, daten: dict):
    tmp = pfad.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=1))
    tmp.chmod(0o600)
    tmp.replace(pfad)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- Passwörter und TOTP (nur Standardbibliothek) ---

def pw_hash(pw: str) -> str:
    salz = secrets.token_bytes(16)
    return salz.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salz, n=2**14, r=8, p=1).hex()


def pw_pruefen(pw: str, gespeichert: str) -> bool:
    salz, wert = gespeichert.split("$")
    return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salz), n=2**14, r=8, p=1).hex(), wert)


def _totp(geheim: str, schritt: int) -> str:
    h = hmac.new(base64.b32decode(geheim), struct.pack(">Q", schritt), "sha1").digest()
    o = h[-1] & 15
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def _totp_schritt(geheim: str, code: str, zuletzt: int) -> int | None:
    """Gültiger Schritt (±30 s Toleranz), der noch nicht benutzt wurde, sonst None."""
    jetzt = int(time.time()) // 30
    for s in (jetzt - 1, jetzt, jetzt + 1):
        if s > zuletzt and hmac.compare_digest(_totp(geheim, s), code.strip().replace(" ", "")):
            return s
    return None


# --- Schutz gegen Passwortraten ---

def bremse(request: Request):
    ip = request.client.host if request.client else "?"
    _fehlversuche[ip] = [t for t in _fehlversuche.get(ip, []) if t > time.time() - 900]
    if len(_fehlversuche[ip]) >= 5:
        raise HTTPException(429, "Zu viele Fehlversuche, bitte 15 Minuten warten.")
    return ip


def fehlschlag(ip: str, text="Name, Passwort oder Code falsch."):
    _fehlversuche.setdefault(ip, []).append(time.time())
    time.sleep(1)
    raise HTTPException(401, text)


# --- Ersteinrichtung ---

def eingerichtet() -> bool:
    return bool(_laden(BENUTZER))


def einrichtungscode() -> str | None:
    """Erzeugt beim ersten Start einen Code, der nur auf dem Server liegt."""
    if eingerichtet():
        CODE.unlink(missing_ok=True)
        return None
    if not CODE.exists():
        CODE.write_text(secrets.token_hex(8))
        CODE.chmod(0o600)
    return CODE.read_text().strip()


# --- Benutzer ---

def benutzer_anlegen(name: str, pw: str, admin: bool):
    name = name.strip().lower()
    if not name.isalnum() or len(name) > 32:
        raise HTTPException(400, "Name nur aus Buchstaben und Ziffern.")
    if len(pw) < 10:
        raise HTTPException(400, "Passwort mindestens 10 Zeichen.")
    with _sperre:
        alle = _laden(BENUTZER)
        if name in alle:
            raise HTTPException(409, "Name ist schon vergeben.")
        alle[name] = {"pw": pw_hash(pw), "admin": admin, "totp": None, "totp_schritt": 0, "token": None}
        _speichern(BENUTZER, alle)
    return name


def benutzer_liste() -> list[dict]:
    return [{"name": n, "admin": b["admin"], "zwei_faktor": bool(b["totp"]), "token": bool(b["token"])}
            for n, b in sorted(_laden(BENUTZER).items())]


def benutzer_aendern(name: str, **werte):
    with _sperre:
        alle = _laden(BENUTZER)
        if name not in alle:
            raise HTTPException(404, "Unbekannter Benutzer.")
        if werte.get("loeschen"):
            if alle[name]["admin"] and sum(b["admin"] for b in alle.values()) == 1:
                raise HTTPException(400, "Der letzte Admin kann nicht gelöscht werden.")
            del alle[name]
        else:
            alle[name].update(werte)
        _speichern(BENUTZER, alle)
    if werte.get("loeschen") or "pw" in werte or werte.get("totp", 1) is None:
        sitzungen_beenden(name)


# --- Sitzungen ---

def anmelden(name: str, pw: str, code: str, ip: str) -> tuple[str, bool]:
    """Gibt (Sitzungs-ID, voll) zurück. voll=False heißt: 2FA muss erst eingerichtet werden."""
    name = name.strip().lower()
    b = _laden(BENUTZER).get(name)
    if not b or not pw_pruefen(pw, b["pw"]):
        if not b:
            pw_hash(pw)  # gleiche Rechenzeit, damit Namen nicht erratbar sind
        fehlschlag(ip)
    voll = False
    if b["totp"]:
        schritt = _totp_schritt(b["totp"], code or "", b["totp_schritt"])
        if schritt is None:
            fehlschlag(ip)
        benutzer_aendern(name, totp_schritt=schritt)
        voll = True
    return sitzung_neu(name, voll), voll


def sitzung_neu(name: str, voll: bool) -> str:
    sid = secrets.token_urlsafe(32)
    with _sperre:
        alle = {k: v for k, v in _laden(SITZUNGEN).items() if v["ablauf"] > time.time()}
        alle[_h(sid)] = {"name": name, "voll": voll, "ablauf": time.time() + SITZUNG_TAGE * 86400}
        _speichern(SITZUNGEN, alle)
    return sid


def sitzung_aendern(sid: str, **werte):
    with _sperre:
        alle = _laden(SITZUNGEN)
        if werte.get("loeschen"):
            alle.pop(_h(sid), None)
        elif _h(sid) in alle:
            alle[_h(sid)].update(werte)
        _speichern(SITZUNGEN, alle)


def sitzungen_beenden(name: str):
    with _sperre:
        _speichern(SITZUNGEN, {k: v for k, v in _laden(SITZUNGEN).items() if v["name"] != name})


def wer(request: Request) -> tuple[dict | None, dict | None]:
    """(Benutzer, Sitzung) aus Cookie oder Bearer-Token."""
    alle = _laden(BENUTZER)
    kopf = request.headers.get("authorization", "")
    if kopf.startswith("Bearer "):
        h = _h(kopf[7:])
        for name, b in alle.items():
            if b["token"] and hmac.compare_digest(b["token"], h):
                return {"name": name, **b}, {"voll": True}
        return None, None
    s = _laden(SITZUNGEN).get(_h(request.cookies.get("sitzung", "")))
    if not s or s["ablauf"] < time.time() or s["name"] not in alle:
        return None, None
    return {"name": s["name"], **alle[s["name"]]}, s


# --- 2FA einrichten ---

def totp_start(sid: str, name: str) -> dict:
    geheim = base64.b32encode(secrets.token_bytes(20)).decode()
    sitzung_aendern(sid, totp_neu=geheim)
    uri = f"otpauth://totp/Reelkoch:{quote(name)}?secret={geheim}&issuer=Reelkoch"
    return {"geheim": geheim, "qr": segno.make(uri).svg_inline(scale=5, border=2)}


def totp_bestaetigen(sid: str, sitzung: dict, name: str, code: str, ip: str):
    geheim = sitzung.get("totp_neu")
    schritt = geheim and _totp_schritt(geheim, code, 0)
    if not schritt:
        fehlschlag(ip, "Code stimmt nicht. Uhrzeit am Handy prüfen.")
    benutzer_aendern(name, totp=geheim, totp_schritt=schritt)
    sitzungen_beenden(name)  # andere Sitzungen abmelden
    return sitzung_neu(name, True)


def token_neu(name: str) -> str:
    token = secrets.token_urlsafe(32)
    benutzer_aendern(name, token=_h(token))
    return token
