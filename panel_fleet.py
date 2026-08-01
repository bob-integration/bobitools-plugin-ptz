# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Parc de PUPITRES : persistance et surveillance de disponibilité.

Jumeau allégé de `fleet.py`. Un pupitre n'a ni mémoires ni instantanés — juste une identité
d'accès (adresse, identifiants) et une table d'affectation qu'on lit/écrit à la demande.
Un seul fichier dans le volume `/data` : `panels.json`.

Même règle de concurrence que pour les caméras (héritée de switch_ports) : le verrou disque
n'est JAMAIS tenu pendant une I/O réseau. Un pupitre injoignable met `timeout` secondes à
répondre ; verrou pris, il figerait tout l'outil.
"""
import json
import os
import threading
import time

import panels

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PANELS_FILE = os.path.join(DATA_DIR, "panels.json")

POLL_INTERVAL = max(5, int(os.environ.get("POLL_INTERVAL") or 20))
POLL_WORKERS = 4

DEFAULTS = {
    "user": os.environ.get("DEFAULT_USER") or "",
    "password": os.environ.get("DEFAULT_PASSWORD") or "",
}

_io = threading.Lock()
_status = {}                # {panel_id: {reachable, latency_ms, checked_at, error}}
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
    os.replace(tmp, path)   # atomique sur POSIX


def load_panels():
    with _io:
        p = _read(PANELS_FILE, [])
        return p if isinstance(p, list) else []


def save_panels(items):
    with _io:
        _write(PANELS_FILE, items)


def find(items, panel_id):
    return next((p for p in items if p.get("id") == panel_id), None)


def get(panel_id):
    return find(load_panels(), panel_id)


# ------------------------------------------------------------------ boutons → numéros
# Le pupitre matériel ne connaît que des IP par bouton. Mais l'affectation VOULUE est en
# NUMÉROS de caméra (indirection : bouton → numéro → caméra → IP). On persiste donc, par
# pupitre, la table bouton → numéro : c'est elle qui fait autorité, l'IP écrite au pupitre
# n'en est que la projection. Sert la propagation : renuméroter une caméra ré-écrit les
# boutons portant ce numéro.

def get_buttons(panel_id):
    """{bouton(int): numéro(int)} pour un pupitre."""
    p = get(panel_id) or {}
    raw = p.get("buttons") or {}
    out = {}
    for k, v in raw.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            pass
    return out


def set_button(panel_id, button, number):
    """Fixe (ou efface si `number` est None) le numéro affecté à un bouton. Persistant."""
    with _io:
        items = _read(PANELS_FILE, [])
        if not isinstance(items, list):
            return
        p = find(items, panel_id)
        if not p:
            return
        btns = p.get("buttons")
        if not isinstance(btns, dict):
            btns = {}
        key = str(int(button))
        if number is None:
            btns.pop(key, None)
        else:
            btns[key] = int(number)
        p["buttons"] = btns
        _write(PANELS_FILE, items)


def buttons_for_number(number):
    """Tous les (panel_id, bouton) qui portent ce numéro — pour la propagation."""
    out = []
    for p in load_panels():
        for k, v in (p.get("buttons") or {}).items():
            try:
                if int(v) == int(number):
                    out.append((p.get("id"), int(k)))
            except (TypeError, ValueError):
                pass
    return out


# --------------------------------------------------------------------------- pilotes

def driver_for(panel):
    """Instancie le pilote d'un pupitre, identifiants du parc ou valeurs par défaut."""
    return panels.build(panel, DEFAULTS)


def status_of(panel_id):
    with _status_lock:
        st = _status.get(panel_id)
        return dict(st) if st else {"reachable": None, "latency_ms": None,
                                    "checked_at": None, "error": None}


def _probe_one(panel):
    """Sonde un pupitre. HORS de tout verrou disque."""
    pid = panel.get("id")
    t0 = time.monotonic()
    reachable, error = False, None
    try:
        drv = driver_for(panel)
        reachable = bool(drv.reachable())
        if not reachable:
            error = drv.last_error or "sans réponse"
    except panels.PanelError as e:
        error = str(e)
    except Exception as e:                          # noqa: BLE001
        error = f"pilote : {e}"
    st = {"reachable": reachable, "latency_ms": int((time.monotonic() - t0) * 1000),
          "checked_at": time.time(), "error": None if reachable else error}
    with _status_lock:
        _status[pid] = st


def poll_once():
    items = [p for p in load_panels() if p.get("enabled", True)]
    if not items:
        return
    queue = list(items)
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not queue:
                    return
                panel = queue.pop()
            _probe_one(panel)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(POLL_WORKERS, len(queue)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def start_poller():
    def loop():
        while True:
            try:
                poll_once()
            except Exception as e:                  # noqa: BLE001
                print(f"ptz(pupitres) : tour de surveillance en échec : {e}", flush=True)
            time.sleep(POLL_INTERVAL)

    threading.Thread(target=loop, daemon=True).start()


def forget(panel_id):
    with _status_lock:
        _status.pop(panel_id, None)
