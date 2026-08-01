# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Parc de caméras : persistance, nommage local des mémoires, surveillance de disponibilité.

Trois fichiers dans le volume `/data` :
  `cameras.json`      le parc (nom, pilote, adresse, identifiants, groupe)
  `preset_names.json` les noms de mémoires TENUS PAR L'OUTIL, par caméra
  `identity.json`     la dernière identité relevée (modèle, firmware) — simple cache

## Pourquoi un nommage local des mémoires

Aucune commande publique du protocole AW ne nomme les mémoires *dans* la caméra, et une
HE130 n'en serait de toute façon pas capable. Nommer localement donne le service demandé
(retrouver « Plateau large » plutôt que « mémoire 7 ») sur TOUT le parc, tout de suite, et
sans dépendre d'une génération. Si `probe()` révèle un jour une commande de nommage
embarqué, le serveur poussera en plus — le stockage local restera la source affichée.

## Concurrence

Règle reprise de switch_ports, apprise à ses dépens : le verrou disque n'est JAMAIS tenu
pendant une I/O réseau. Une caméra éteinte met `timeout` secondes à répondre ; si le verrou
était pris pendant ce temps, une seule caméra HS gèlerait tout l'outil.
"""
import json
import os
import threading
import time
import uuid

import drivers

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CAMERAS_FILE = os.path.join(DATA_DIR, "cameras.json")
NAMES_FILE = os.path.join(DATA_DIR, "preset_names.json")
IDENTITY_FILE = os.path.join(DATA_DIR, "identity.json")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "snapshots")
PRESTAS_FILE = os.path.join(DATA_DIR, "prestas.json")
SHOTBOX_FILE = os.path.join(DATA_DIR, "shotboxes.json")

POLL_INTERVAL = max(5, int(os.environ.get("POLL_INTERVAL") or 20))
POLL_WORKERS = 6            # sondages simultanés — assez pour un gros parc, doux pour le réseau

DEFAULTS = {
    "user": os.environ.get("DEFAULT_USER") or "",
    "password": os.environ.get("DEFAULT_PASSWORD") or "",
}

_io = threading.Lock()
_status = {}                # {cam_id: {reachable, latency_ms, checked_at, error}}
_status_lock = threading.Lock()


# --------------------------------------------------------------------------- persistance

def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _write(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)   # atomique sur POSIX : jamais de parc à moitié écrit


def load_cameras():
    with _io:
        c = _read(CAMERAS_FILE, [])
        return c if isinstance(c, list) else []


def save_cameras(cams):
    with _io:
        _write(CAMERAS_FILE, cams)


def find(cams, cam_id):
    return next((c for c in cams if c.get("id") == cam_id), None)


def get(cam_id):
    return find(load_cameras(), cam_id)


# --------------------------------------------------------------------------- profils de presta
# Le parc est FIXE ; une « presta » = un jeu nommé d'affectations caméra → numéro. Une caméra
# sans numéro est « non utilisée ». Les profils sont indexés par HÔTE (stable, portable d'une
# instance à l'autre) plutôt que par id interne. Charger un profil pose les numéros du profil
# et RETIRE le numéro des caméras absentes du profil (elles redeviennent non utilisées).

def load_prestas():
    with _io:
        d = _read(PRESTAS_FILE, {})
        if not isinstance(d, dict):
            d = {}
        d.setdefault("active", None)
        d.setdefault("profiles", {})
        return d


def presta_names():
    return sorted(load_prestas()["profiles"].keys())


def save_presta(name):
    """Enregistre la numérotation COURANTE sous `name` (host → numéro) et la marque active."""
    nums = {}
    for c in load_cameras():                          # hors verrou (load_cameras verrouille)
        h = (c.get("host") or "").strip()
        if h and c.get("cam_number") is not None:
            nums[h] = c["cam_number"]
    with _io:
        d = _read(PRESTAS_FILE, {})
        if not isinstance(d, dict):
            d = {}
        d.setdefault("profiles", {})[name] = nums
        d["active"] = name
        _write(PRESTAS_FILE, d)
    return nums


def apply_presta(name):
    """Applique un profil : pose ses numéros, retire ceux des caméras hors profil."""
    prof = load_prestas()["profiles"].get(name)
    if prof is None:
        return False
    with _io:
        cams = _read(CAMERAS_FILE, [])
        if not isinstance(cams, list):
            cams = []
        for c in cams:
            c["cam_number"] = prof.get((c.get("host") or "").strip())
        _write(CAMERAS_FILE, cams)
        d = _read(PRESTAS_FILE, {})
        if isinstance(d, dict):
            d["active"] = name
            _write(PRESTAS_FILE, d)
    return True


def delete_presta(name):
    with _io:
        d = _read(PRESTAS_FILE, {})
        if not isinstance(d, dict) or name not in d.get("profiles", {}):
            return False
        d["profiles"].pop(name, None)
        if d.get("active") == name:
            d["active"] = None
        _write(PRESTAS_FILE, d)
        return True


# --------------------------------------------------------------------------- shotboxes
# Grilles de boutons paramétrables (rappel de mémoire d'une caméra, ou lancement d'une macro),
# nommées. La config seule est stockée ici ; le DÉCLENCHEMENT réutilise les routes existantes
# (recall_preset côté caméra, macro_control côté pupitre).

def load_shotboxes():
    with _io:
        d = _read(SHOTBOX_FILE, {})
        return d if isinstance(d, dict) else {}


def save_shotbox(name, box):
    with _io:
        d = _read(SHOTBOX_FILE, {})
        if not isinstance(d, dict):
            d = {}
        d[name] = box
        _write(SHOTBOX_FILE, d)


def delete_shotbox(name):
    with _io:
        d = _read(SHOTBOX_FILE, {})
        if isinstance(d, dict) and d.pop(name, None) is not None:
            _write(SHOTBOX_FILE, d)
            return True
    return False


# --------------------------------------------------------------------------- export / import
# Le parc et les profils s'exportent en un fichier JSON (SANS mots de passe : garde-fou
# anti-secret du projet). L'import fait un UPSERT par hôte (ajoute/actualise, ne supprime
# jamais) : le mot de passe et l'id d'une caméra déjà présente sont préservés.

def export_data():
    cams = [{k: v for k, v in c.items() if k != "password"} for c in load_cameras()]
    return {"cameras": cams, "prestas": load_prestas().get("profiles", {}),
            "shotboxes": load_shotboxes()}


def import_data(data):
    incoming = (data or {}).get("cameras") or []
    added, updated = 0, 0
    with _io:
        cur = _read(CAMERAS_FILE, [])
        if not isinstance(cur, list):
            cur = []
        by_host = {(c.get("host") or "").strip(): c for c in cur}
        for ic in incoming:
            h = (ic.get("host") or "").strip()
            if not h:
                continue
            existing = by_host.get(h)
            if existing:
                pw = existing.get("password")
                existing.update({k: v for k, v in ic.items() if k != "password"})
                existing["id"] = existing.get("id") or uuid.uuid4().hex[:12]
                if pw:
                    existing["password"] = pw          # l'export n'a pas le mot de passe
                updated += 1
            else:
                rec = {k: v for k, v in ic.items() if k != "password"}
                rec["id"] = rec.get("id") or uuid.uuid4().hex[:12]
                cur.append(rec)
                by_host[h] = rec
                added += 1
        _write(CAMERAS_FILE, cur)
        prof = (data or {}).get("prestas") or {}
        if isinstance(prof, dict) and prof:
            d = _read(PRESTAS_FILE, {})
            if not isinstance(d, dict):
                d = {"active": None, "profiles": {}}
            d.setdefault("profiles", {}).update(prof)
            _write(PRESTAS_FILE, d)
        boxes = (data or {}).get("shotboxes") or {}
        if isinstance(boxes, dict) and boxes:
            sb = _read(SHOTBOX_FILE, {})
            if not isinstance(sb, dict):
                sb = {}
            sb.update(boxes)
            _write(SHOTBOX_FILE, sb)
    return {"added": added, "updated": updated}


# --------------------------------------------------------------------------- noms de mémoires

def load_names():
    with _io:
        n = _read(NAMES_FILE, {})
        return n if isinstance(n, dict) else {}


def names_for(cam_id):
    """{"7": "Plateau large", …} — clés en CHAÎNES (contrainte JSON), converties à l'usage."""
    return (load_names().get(cam_id) or {})


def set_name(cam_id, index, name):
    with _io:
        all_names = _read(NAMES_FILE, {})
        if not isinstance(all_names, dict):
            all_names = {}
        entry = all_names.setdefault(cam_id, {})
        key = str(int(index))
        if (name or "").strip():
            entry[key] = name.strip()[:64]
        else:
            entry.pop(key, None)        # nom vidé = mémoire redevenue anonyme
        if not entry:
            all_names.pop(cam_id, None)
        _write(NAMES_FILE, all_names)


def drop_names(cam_id):
    with _io:
        all_names = _read(NAMES_FILE, {})
        if isinstance(all_names, dict) and all_names.pop(cam_id, None) is not None:
            _write(NAMES_FILE, all_names)


# --------------------------------------------------------------------------- identités

def load_identities():
    with _io:
        i = _read(IDENTITY_FILE, {})
        return i if isinstance(i, dict) else {}


def set_identity(cam_id, ident):
    with _io:
        all_id = _read(IDENTITY_FILE, {})
        if not isinstance(all_id, dict):
            all_id = {}
        all_id[cam_id] = dict(ident or {}, checked_at=time.time())
        _write(IDENTITY_FILE, all_id)


def drop_identity(cam_id):
    with _io:
        all_id = _read(IDENTITY_FILE, {})
        if isinstance(all_id, dict) and all_id.pop(cam_id, None) is not None:
            _write(IDENTITY_FILE, all_id)


# --------------------------------------------------------------------------- pilotes

def driver_for(cam):
    """Instancie le pilote d'une caméra, identifiants du parc ou valeurs par défaut.

    Si le modèle n'a pas été saisi à la main, on retombe sur celui RELEVÉ par
    l'identification. Sans ça, un pilote dont le comportement dépend de la génération
    (tables de formats vidéo côté Panasonic) retomberait sur sa famille par défaut et
    proposerait des valeurs qui ne sont pas celles de la caméra — silencieusement."""
    if not cam.get("model"):
        detected = (load_identities().get(cam.get("id")) or {}).get("model")
        if detected:
            cam = dict(cam, model=detected)
    return drivers.build(cam, DEFAULTS)


def status_of(cam_id):
    with _status_lock:
        st = _status.get(cam_id)
        return dict(st) if st else {"reachable": None, "latency_ms": None,
                                    "checked_at": None, "error": None}


def _probe_one(cam):
    """Sonde une caméra. HORS de tout verrou disque (cf. en-tête du module)."""
    cam_id = cam.get("id")
    t0 = time.monotonic()
    reachable, error = False, None
    try:
        drv = driver_for(cam)
        reachable = bool(drv.reachable())
        if not reachable:
            # Message précis quand le pilote en a un (« authentification refusée » ne se
            # dépanne pas comme « injoignable »).
            error = drv.last_error or "sans réponse"
    except drivers.DriverError as e:
        error = str(e)
    except Exception as e:                      # noqa: BLE001 — un pilote ne doit pas tuer la boucle
        error = f"pilote : {e}"
    st = {"reachable": reachable, "latency_ms": int((time.monotonic() - t0) * 1000),
          "checked_at": time.time(), "error": None if reachable else error}
    with _status_lock:
        _status[cam_id] = st


def poll_once():
    """Un tour de surveillance, en parallèle borné."""
    cams = [c for c in load_cameras() if c.get("enabled", True)]
    if not cams:
        return
    queue = list(cams)
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not queue:
                    return
                cam = queue.pop()
            _probe_one(cam)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(POLL_WORKERS, len(queue)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def start_poller():
    """Boucle de surveillance de fond. Une exception n'arrête jamais la boucle : un parc
    injoignable doit rester signalé comme tel, pas faire disparaître la surveillance."""
    def loop():
        while True:
            try:
                poll_once()
            except Exception as e:              # noqa: BLE001
                print(f"ptz : tour de surveillance en échec : {e}", flush=True)
            time.sleep(POLL_INTERVAL)

    threading.Thread(target=loop, daemon=True).start()


def forget(cam_id):
    """Purge l'état vivant associé à une caméra supprimée.

    Les INSTANTANÉS ne sont pas touchés : ils décrivent un état passé du parc, et effacer
    l'historique d'une caméra parce qu'elle a été retirée priverait justement de la
    comparaison « qu'est-ce qui a changé depuis ». Un instantané qui référence une caméra
    disparue le signale à la comparaison, il ne se corrompt pas."""
    with _status_lock:
        _status.pop(cam_id, None)
    drop_names(cam_id)
    drop_identity(cam_id)


# --------------------------------------------------------------------------- instantanés

# Un fichier par instantané plutôt qu'un gros fichier unique : en créer un n'oblige pas à
# réécrire les précédents, et un fichier corrompu n'emporte pas tout l'historique.

def _snap_path(sid):
    return os.path.join(SNAPSHOT_DIR, f"{sid}.json")


def list_snapshots():
    """Résumés, du plus récent au plus ancien (le contenu complet n'est pas chargé)."""
    with _io:
        try:
            files = os.listdir(SNAPSHOT_DIR)
        except FileNotFoundError:
            return []
        out = []
        for fn in files:
            if not fn.endswith(".json"):
                continue
            snap = _read(os.path.join(SNAPSHOT_DIR, fn), None)
            if not isinstance(snap, dict):
                continue
            out.append({
                "id": snap.get("id"),
                "name": snap.get("name"),
                "created_at": snap.get("created_at"),
                "count": len(snap.get("cameras") or []),
                "note": snap.get("note") or "",
            })
    return sorted(out, key=lambda s: s.get("created_at") or 0, reverse=True)


def get_snapshot(sid):
    with _io:
        snap = _read(_snap_path(sid), None)
    return snap if isinstance(snap, dict) else None


def save_snapshot(snap):
    with _io:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        _write(_snap_path(snap["id"]), snap)
    return snap


def delete_snapshot(sid):
    with _io:
        try:
            os.remove(_snap_path(sid))
            return True
        except FileNotFoundError:
            return False
