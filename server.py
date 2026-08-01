# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Serveur de l'outil « Caméras tourelles » (runtime=docker).

HTTP minimal (ThreadingHTTPServer, stdlib), proxifié par l'app sous /api/tools/ptz/*.

Le serveur ne connaît AUCUN protocole de caméra : il orchestre le parc, la surveillance et
les actions groupées, et délègue tout le reste au pilote de la marque (cf. `drivers/base.py`).
Ajouter Sony ou Canon ne touche pas une ligne de ce fichier.

## Actions groupées

C'est la raison d'être de l'outil : appliquer un réglage (format vidéo…) ou rappeler une
mémoire sur N caméras d'un coup. Deux règles :

- **exécution en parallèle borné** — sinon 20 caméras × 4 s de timeout = 80 s d'attente ;
- **compte-rendu par caméra, jamais un verdict global** — une action groupée réussit
  presque toujours *partiellement*. Annoncer « OK » quand 3 caméras sur 12 ont échoué
  serait le pire service à rendre en exploitation. Chaque ligne du rapport porte son
  propre succès et son propre message d'erreur.
"""
import json
import os
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import drivers
import fleet
import panels
import panel_fleet

PORT = 8080
BULK_WORKERS = 8            # actions groupées simultanées
MAX_BATCH = 64              # garde-fou de l'ajout en série


def _env_int(name, default):
    """Entier de réglage lu de l'environnement, borné à ≥ 1 ; défaut si absent ou invalide."""
    try:
        return max(1, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


# Dimensions de la GRILLE, réglables (config_schema → env_settings). Permettent de faire
# évoluer l'échelle sans toucher au code : nombre de caméras (lignes / numéro max), de
# pupitres (blocs) et de boutons par pupitre (colonnes par bloc) exposés.
GRID_CAMERAS = _env_int("GRID_CAMERAS", 100)
GRID_PANELS = _env_int("GRID_PANELS", 10)
GRID_BUTTONS = _env_int("GRID_BUTTONS", 10)

# Champs du parc modifiables par l'UI (liste blanche : rien d'autre n'entre en base).
FIELDS = ("name", "driver", "host", "port", "user", "password", "model",
          "group", "notes", "enabled", "timeout", "cam_number")


# --------------------------------------------------------------------------- helpers

def _public(cam, idents=None):
    """Vue d'une caméra destinée au navigateur. Le mot de passe n'en sort JAMAIS : on
    n'expose qu'un booléen disant qu'il est renseigné.

    `idents` permet à un appelant qui liste tout le parc de charger les identités UNE fois
    au lieu d'une par caméra (chaque lecture prend le verrou disque)."""
    st = fleet.status_of(cam.get("id"))
    if idents is None:
        idents = fleet.load_identities()
    ident = idents.get(cam.get("id")) or {}
    caps, outs, prange = [], [], [1, 100]
    driver_ok, driver_err = True, None
    try:
        drv = fleet.driver_for(cam)
        caps = sorted(drv.capabilities())
        outs = drv.outputs()
        prange = list(type(drv).PRESET_RANGE)
    except drivers.DriverError as e:
        driver_ok, driver_err = False, str(e)
    return {
        "id": cam.get("id"),
        "name": cam.get("name"),
        "driver": cam.get("driver"),
        "host": cam.get("host"),
        "port": cam.get("port"),
        "user": cam.get("user") or "",
        "has_password": bool(cam.get("password")),
        "model": cam.get("model") or "",
        "group": cam.get("group") or "",
        "notes": cam.get("notes") or "",
        "enabled": cam.get("enabled", True),
        "timeout": cam.get("timeout") or 4,
        "cam_number": cam.get("cam_number"),
        "identity": ident,
        "capabilities": caps,
        # Sorties et plage de mémoires : déclarées par le pilote, AUCUNE I/O caméra. C'est
        # ce qui permet d'afficher les colonnes et les lignes des vues transverses tout de
        # suite, puis d'y verser les valeurs au fur et à mesure.
        "outputs": outs,
        "preset_range": prange,
        "driver_ok": driver_ok,
        "driver_error": driver_err,
        "reachable": st["reachable"],
        "latency_ms": st["latency_ms"],
        "checked_at": st["checked_at"],
        "error": st["error"],
    }


def _apply_fields(cam, body):
    for f in FIELDS:
        if f not in body:
            continue
        v = body[f]
        if f == "port":
            cam[f] = int(v) if v else None
        elif f == "enabled":
            cam[f] = bool(v)
        elif f == "timeout":
            cam[f] = max(1, min(30, float(v or 4)))
        elif f == "cam_number":
            # Numéro d'exploitation de la caméra. INDÉPENDANT du slot pupitre (plusieurs
            # cadreurs se répartissent les caméras → pas de lien fixe). Optionnel : vide = None.
            if v in (None, ""):
                cam[f] = None
            else:
                try:
                    cam[f] = int(v)
                except (TypeError, ValueError):
                    cam[f] = None
        elif f == "password":
            # Une chaîne vide venant de l'UI signifie « ne change rien » : le mot de passe
            # n'est jamais renvoyé au navigateur, il ne peut donc pas être reposté tel quel.
            if v:
                cam[f] = str(v)
        else:
            cam[f] = ("" if v is None else str(v)).strip()
    return cam


def _cam_number_conflict(cams, cam_id, number):
    """Nom de la caméra qui porte DÉJÀ ce numéro (hors `cam_id`), ou None. Un numéro doit
    être unique dans le parc pour que le tri et le pré-remplissage par numéro aient un sens ;
    None (pas de numéro) n'entre jamais en conflit."""
    if number is None:
        return None
    for c in cams:
        if c.get("id") != cam_id and c.get("cam_number") == number:
            return c.get("name") or c.get("host") or c.get("id")
    return None


def _ip_range(start, count):
    """[start, start+1, …] sur le DERNIER octet seulement.

    On refuse de déborder au-delà de .254 plutôt que de repasser sur l'octet supérieur :
    une série qui traverse un sous-réseau n'est jamais ce que l'utilisateur voulait, et
    créerait des entrées pointant vers d'autres machines."""
    parts = start.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        raise ValueError(f"adresse IPv4 invalide : {start}")
    base, last = ".".join(parts[:3]), int(parts[3])
    if last + count - 1 > 254:
        raise ValueError(f"la série dépasse {base}.254 — réduisez le nombre "
                         f"ou changez l'adresse de départ")
    return [f"{base}.{last + i}" for i in range(count)]


def _okey(output_value):
    """Clé du paramètre « format » d'une sortie ("" = réglage général)."""
    return "video_format" + (("_" + output_value) if output_value else "")


def _freq_label(cam, code):
    """Libellé de la fréquence tel que le pilote le définit — le nombre de fréquences
    dépend du modèle (2 sur AW-HE130, 5 sur AW-UE160), donc c'est au pilote de trancher."""
    if code is None:
        return None
    try:
        drv = fleet.driver_for(cam)
        labels = drv.freq_labels() if hasattr(drv, "freq_labels") else {}
        return labels.get(str(code), str(code))
    except Exception:                                # noqa: BLE001
        return str(code)


def _require_reachable(drv):
    """Sonde la caméra UNE fois avant toute lecture en masse.

    Sans ce garde, une caméra éteinte fait subir un délai d'attente à CHACUNE des lectures
    qui suivent — huit pour la vue d'ensemble, une centaine pour les noms de mémoires, une
    douzaine pour un instantané. Une seule machine hors service suffisait alors à figer la
    vue entière pendant des dizaines de secondes.

    Le sondage est fait en direct plutôt que lu dans le cache de surveillance : ce dernier
    peut avoir jusqu'à un intervalle de retard, et déclarer injoignable une caméra qui vient
    de revenir serait aussi faux que l'inverse."""
    if not drv.reachable():
        raise drivers.DriverError(getattr(drv, "last_error", None) or "injoignable")
    return drv


def _slim_schema(schema):
    """Schéma réduit conservé DANS l'instantané. Il fige les libellés et l'inscriptibilité
    au moment de la capture, pour qu'un instantané reste lisible même si la caméra a
    disparu du parc — mais la restauration, elle, se fie toujours au schéma COURANT."""
    return [{"key": p["key"], "label": p["label"], "type": p["type"],
             "writable": p["writable"], "order": p.get("order", 50),
             "heavy": p.get("heavy", False)} for p in schema]


def _capture_one(cam):
    """Relevé d'une caméra pour un instantané. On relève TOUT ce qui est lisible, y compris
    ce qu'on ne saura pas réécrire : ça ne coûte rien et c'est ce qui rend la comparaison
    utile là où la restauration ne l'est pas."""
    drv = _require_reachable(fleet.driver_for(cam))
    schema = drv.params_schema()
    values = drv.read_params()
    # Un pilote renvoie `None` par paramètre illisible plutôt que d'échouer, pour qu'une
    # valeur manquante n'emporte pas les autres. Mais si TOUT est `None`, la caméra n'a
    # rien dit du tout : l'enregistrer comme un relevé réussi donnerait un instantané qui
    # prétend couvrir cette caméra alors qu'il ne contient rien d'elle.
    if not any(v is not None for v in values.values()):
        raise drivers.DriverError("aucun paramètre lisible (caméra injoignable ?)")
    return {
        "id": cam.get("id"), "name": cam.get("name"), "host": cam.get("host"),
        "driver": cam.get("driver"), "model": cam.get("model") or "",
        "values": values,
        "schema": _slim_schema(schema),
    }


def _run_bulk(cams, fn):
    """Exécute `fn(cam)` sur chaque caméra en parallèle borné et renvoie un compte-rendu
    ligne à ligne. `fn` peut lever : l'échec est capté et rattaché à SA caméra."""
    results = [None] * len(cams)
    queue = list(range(len(cams)))
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not queue:
                    return
                i = queue.pop()
            cam = cams[i]
            row = {"id": cam.get("id"), "name": cam.get("name"), "ok": False,
                   "error": None, "detail": None}
            try:
                row["detail"] = fn(cam)
                row["ok"] = True
            except drivers.DriverError as e:
                row["error"] = str(e)
            except Exception as e:                  # noqa: BLE001
                row["error"] = f"erreur interne : {e}"
            results[i] = row

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(BULK_WORKERS, max(1, len(cams))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = sum(1 for r in results if r and r["ok"])
    return {"results": results, "count_ok": ok, "count_fail": len(results) - ok}


# --------------------------------------------------------------------------- Ember+
# Contribution au SERVICE Ember+ global (services/emberplus). L'arbre `ember/tree` doit se
# construire SANS I/O caméra : le service le balaie toutes les 5 s et saute un contributeur
# qui met plus de 10 s. Un rafraîchisseur de fond LENT (EMBER_POLL) relève les valeurs
# pilotables (marche/veille, format vidéo) des caméras JOIGNABLES et les met en cache ;
# l'arbre lit le cache, les écritures partent en direct et rafraîchissent la caméra touchée.
EMBER_POLL = max(10, int(os.environ.get("EMBER_POLL") or 30))
_ember_vals = {}                 # {cam_id: {power, format, format_options, preset_range}}
_ember_panel_vals = {}           # {panel_id: {pos: cam_id|None}} — affectation des boutons
_ember_src = {}                  # {n° de source Ember+ : cam_id} — reconstruit à chaque arbre
_ember_lock = threading.Lock()


# Numérotation des CIBLES de la matrice (colonnes), telle que demandée : 1..GRID_CAMERAS =
# numéros de caméra ; puis GRID_BUTTONS cibles par pupitre (pupitre 1 → GRID_CAMERAS+1.., etc.).
def _ember_panel_target(panel_index, button):     # panel_index : 0-based
    return GRID_CAMERAS + panel_index * GRID_BUTTONS + button


def _ember_decode_target(target):
    """Cible → ('number', n) ou ('button', panel_index, button) ou (None,)."""
    if 1 <= target <= GRID_CAMERAS:
        return ("number", target)
    off = target - GRID_CAMERAS - 1
    if off >= 0:
        pi, b = divmod(off, GRID_BUTTONS)
        return ("button", pi, b + 1)
    return (None,)


def _ember_get(cam_id):
    with _ember_lock:
        v = _ember_vals.get(cam_id)
        return dict(v) if v else {}


# Métadonnées de la matrice, figées à chaque construction de l'arbre pour interpréter les
# `connect` (le VSM renvoie des n° de source/cible, pas nos identités) : offset des sources
# « numéro » et map n° de source « caméra » → cam_id.
_ember_matrix_meta = {"offset": 0, "camsrc": {}}


def _ceil10(n):
    return ((n + 9) // 10) * 10


def _ember_num_offset(ncams):
    """Début (arrondi) du bloc de sources « numéro de caméra ». Basé sur GRID_CAMERAS,
    arrondi à la dizaine supérieure → 101 / 151 / 201… ; relevé si le parc est assez gros
    pour éviter tout chevauchement avec les sources « caméra » (1 Disconnect + N caméras)."""
    return max(_ceil10(GRID_CAMERAS), _ceil10(ncams + 2))


def _ember_camera_for_number(number):
    if not number:
        return None
    return next((c for c in fleet.load_cameras() if c.get("cam_number") == number), None)


def _ember_push_button(panel, button, number):
    """Écrit UN bouton de pupitre selon le NUMÉRO voulu : résout numéro→caméra→IP et écrit le
    slot (ou NoAssign si le numéro n'est porté par aucune caméra). I/O — hors `ember/tree`."""
    drv = panel_fleet.driver_for(panel)
    cam = _ember_camera_for_number(number)
    if cam:
        drv.write_assignments([{
            "slot": button, "control_type": panels.CTRL_LAN,
            "ip": (cam.get("host") or "").strip(), "port": cam.get("port") or 80,
            "user": cam.get("user") or fleet.DEFAULTS.get("user") or "",
            "password": cam.get("password") or fleet.DEFAULTS.get("password") or "",
        }])
    else:
        drv.write_assignments([{"slot": button, "control_type": panels.CTRL_NONE}])


def _ember_propagate(numbers):
    """Ré-écrit tous les boutons portant l'un de ces numéros (après une renumérotation)."""
    nums = {int(n) for n in numbers if n}
    if not nums:
        return
    by_id = {p.get("id"): p for p in panel_fleet.load_panels()}
    for n in nums:
        for pid, button in panel_fleet.buttons_for_number(n):
            panel = by_id.get(pid)
            if panel:
                try:
                    _ember_push_button(panel, button, n)
                except Exception:                    # noqa: BLE001 — un pupitre muet ne bloque pas
                    pass


def _ember_refresh_one(cam):
    """Relève les valeurs pilotables d'UNE caméra et les met en cache. Fait de l'I/O — donc
    JAMAIS appelé depuis `ember/tree`, seulement par le rafraîchisseur de fond ou juste après
    une écriture. Un échec est avalé : il ne doit pas casser la boucle ni l'écriture."""
    try:
        drv = fleet.driver_for(cam)
        caps = drv.capabilities()
        entry = {}
        want = ["power"] if drivers.CAP_POWER in caps else []
        vf = None
        if drivers.CAP_PARAMS in caps:
            vf = {p["key"]: p for p in drv.params_schema()}.get("video_format")
            if vf and vf.get("writable"):
                want.append("video_format")
        vals = drv.read_params(want) if want else {}
        if "power" in want:
            entry["power"] = vals.get("power")
        if vf and vf.get("writable"):
            entry["format"] = vals.get("video_format")
            entry["format_options"] = [o["label"] for o in vf.get("options", [])]
        if drivers.CAP_PRESETS in caps:
            entry["preset_range"] = list(type(drv).PRESET_RANGE)
        with _ember_lock:
            _ember_vals[cam.get("id")] = entry
    except Exception:                                # noqa: BLE001
        pass


def _ember_poll_once():
    # Seules les caméras/pupitres JOIGNABLES (état de la surveillance) sont relevés : sonder
    # une machine éteinte imposerait un timeout à chaque tour pour rien.
    cams = [c for c in fleet.load_cameras()
            if c.get("enabled", True) and fleet.status_of(c.get("id")).get("reachable")]
    if cams:
        _run_bulk(cams, _ember_refresh_one)          # parallèle borné, échecs isolés
    # Le tally des boutons de pupitre vient du stockage « bouton → numéro » (panel_fleet),
    # pas d'une lecture matérielle : rien à relever ici pour la matrice.


def _ember_start_poller():
    def loop():
        while True:
            try:
                _ember_poll_once()
            except Exception as e:                   # noqa: BLE001
                print(f"ptz(ember) : tour de relevé en échec : {e}", flush=True)
            time.sleep(EMBER_POLL)
    threading.Thread(target=loop, daemon=True).start()


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                        # les logs applicatifs suffisent

    # -- sortie --------------------------------------------------------------

    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail500(self, exc):
        """Dernier rempart : journalise la TRACE (sinon les 500 sont muets et indiagnosticables)
        puis renvoie une 500 avec le message. Le type d'exception aide à cibler la panne."""
        print("ptz : 500 sur %s %s : %r" % (self.command, self.path, exc), flush=True)
        traceback.print_exc()
        return self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def _send_raw(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _cam_or_404(self, cam_id):
        cam = fleet.get(cam_id)
        if not cam:
            self._send(404, {"error": "caméra inconnue"})
            return None
        return cam

    # -- GET -----------------------------------------------------------------

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        query = parse_qs(u.query)
        try:
            if parts == ["health"]:
                return self._send(200, {"ok": True})
            if parts == ["drivers"]:
                return self._send(200, {"drivers": drivers.catalog(),
                                        "poll_interval": fleet.POLL_INTERVAL})
            if parts == ["cameras"]:
                return self._list()
            if parts == ["overview"]:
                return self._overview()
            if parts == ["presets", "matrix"]:
                return self._presets_matrix()
            if parts == ["snapshots"]:
                return self._send(200, {"snapshots": fleet.list_snapshots()})
            if len(parts) == 2 and parts[0] == "snapshots":
                snap = fleet.get_snapshot(parts[1])
                return (self._send(200, {"snapshot": snap}) if snap
                        else self._send(404, {"error": "instantané inconnu"}))
            if len(parts) == 3 and parts[0] == "snapshots" and parts[2] == "compare":
                return self._snapshot_compare(parts[1])
            if len(parts) == 3 and parts[0] == "cameras":
                cam = self._cam_or_404(parts[1])
                if not cam:
                    return None
                if parts[2] == "params":
                    return self._params_read(cam, query)
                if parts[2] == "overview":
                    return self._send(200, self._overview_one(cam))
                if parts[2] == "names":
                    return self._send(200, self._names_one(cam))
                if parts[2] == "presets":
                    return self._presets(cam)
                if parts[2] == "snapshot":
                    return self._snapshot(cam)
                if parts[2] == "network":
                    return self._send(200, fleet.driver_for(cam).read_network())
            # -- profils de presta / export --------------------------------
            if parts == ["prestas"]:
                d = fleet.load_prestas()
                return self._send(200, {"names": sorted(d["profiles"].keys()),
                                        "active": d.get("active")})
            if parts == ["export"]:
                return self._send(200, fleet.export_data())
            if parts == ["shotboxes"]:
                return self._send(200, {"boxes": fleet.load_shotboxes()})
            # -- pupitres --------------------------------------------------
            if parts == ["config"]:
                return self._send(200, {"grid": {"cameras": GRID_CAMERAS,
                                                 "panels": GRID_PANELS,
                                                 "buttons": GRID_BUTTONS}})
            if parts == ["ember", "tree"]:
                return self._ember_tree()
            if parts == ["panels", "drivers"]:
                return self._send(200, {"drivers": panels.catalog(),
                                        "poll_interval": panel_fleet.POLL_INTERVAL})
            if parts == ["panels"]:
                return self._panels_list()
            if len(parts) == 3 and parts[0] == "panels" and parts[2] == "assignments":
                panel = self._panel_or_404(parts[1])
                return None if not panel else self._panel_assignments(panel)
            if len(parts) == 4 and parts[0] == "panels" and parts[2] == "macros":
                panel = self._panel_or_404(parts[1])
                return None if not panel else self._macro_read(panel, parts[3])
            return self._send(404, {"error": "route inconnue"})
        except panels.PanelError as e:
            return self._send(502, {"error": str(e)})
        except drivers.DriverError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:                       # noqa: BLE001 — dernier rempart
            return self._fail500(e)

    # -- POST ----------------------------------------------------------------

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = self._body()
        try:
            if parts == ["cameras"]:
                return self._create(body)
            if parts == ["cameras", "batch"]:
                return self._create_batch(body)
            if parts == ["poll"]:
                fleet.poll_once()
                return self._list()
            if parts == ["discover"]:
                return self._discover(body)
            # -- profils de presta / import --------------------------------
            if parts == ["prestas"]:
                name = (body.get("name") or "").strip()
                if not name:
                    return self._send(400, {"error": "nom de presta requis"})
                fleet.save_presta(name)
                d = fleet.load_prestas()
                return self._send(200, {"ok": True, "names": sorted(d["profiles"].keys()),
                                        "active": d.get("active")})
            if parts == ["prestas", "apply"]:
                if not fleet.apply_presta((body.get("name") or "").strip()):
                    return self._send(404, {"error": "presta inconnue"})
                return self._list()          # renvoie le parc avec les nouveaux numéros
            if parts == ["prestas", "delete"]:
                return (self._send(200, {"ok": True}) if fleet.delete_presta((body.get("name") or "").strip())
                        else self._send(404, {"error": "presta inconnue"}))
            if parts == ["import"]:
                rep = fleet.import_data(body.get("data") if "data" in body else body)
                return self._send(200, {"ok": True, **rep})
            if parts == ["shotboxes"]:
                name = (body.get("name") or "").strip()
                if not name:
                    return self._send(400, {"error": "nom de shotbox requis"})
                fleet.save_shotbox(name, body.get("box") or {})
                return self._send(200, {"ok": True, "boxes": fleet.load_shotboxes()})
            if parts == ["shotboxes", "delete"]:
                return (self._send(200, {"ok": True}) if fleet.delete_shotbox((body.get("name") or "").strip())
                        else self._send(404, {"error": "shotbox inconnue"}))
            if parts == ["snapshots"]:
                return self._snapshot_create(body)
            if len(parts) == 3 and parts[0] == "snapshots" and parts[2] == "restore":
                return self._snapshot_restore(parts[1], body)
            if len(parts) == 2 and parts[0] == "bulk":
                return self._bulk(parts[1], body)
            if len(parts) >= 3 and parts[0] == "cameras":
                cam = self._cam_or_404(parts[1])
                if not cam:
                    return None
                return self._cam_action(cam, parts[2:], body)
            # -- pupitres --------------------------------------------------
            if parts == ["ember", "set"]:
                return self._ember_set(body)
            if parts == ["ember", "connect"]:
                return self._ember_connect(body)
            if parts == ["panels"]:
                return self._panel_create(body)
            if parts == ["panels", "poll"]:
                panel_fleet.poll_once()
                return self._panels_list()
            if len(parts) == 3 and parts[0] == "panels" and parts[2] == "assignments":
                panel = self._panel_or_404(parts[1])
                return None if not panel else self._panel_assign_write(panel, body)
            if len(parts) == 3 and parts[0] == "panels" and parts[2] == "identify":
                panel = self._panel_or_404(parts[1])
                return None if not panel else self._panel_identify(panel)
            if len(parts) == 4 and parts[0] == "panels" and parts[2] == "macros":
                panel = self._panel_or_404(parts[1])
                return None if not panel else self._macro_write(panel, parts[3], body)
            if len(parts) == 5 and parts[0] == "panels" and parts[2] == "macros" \
                    and parts[4] == "control":
                panel = self._panel_or_404(parts[1])
                return None if not panel else self._macro_control(panel, parts[3], body)
            return self._send(404, {"error": "route inconnue"})
        except panels.Unsupported as e:
            return self._send(501, {"error": str(e)})
        except panels.PanelError as e:
            return self._send(502, {"error": str(e)})
        except drivers.Unsupported as e:
            return self._send(501, {"error": str(e)})
        except drivers.DriverError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:                       # noqa: BLE001
            return self._fail500(e)

    def do_PUT(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        body = self._body()
        try:
            if len(parts) == 2 and parts[0] == "cameras":
                return self._update(parts[1], body)
            if len(parts) == 2 and parts[0] == "panels":
                return self._panel_update(parts[1], body)
            return self._send(404, {"error": "route inconnue"})
        except Exception as e:                       # noqa: BLE001
            return self._fail500(e)

    def do_DELETE(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if len(parts) == 2 and parts[0] == "cameras":
                return self._delete(parts[1])
            if len(parts) == 2 and parts[0] == "panels":
                return self._panel_delete(parts[1])
            if len(parts) == 2 and parts[0] == "snapshots":
                return (self._send(200, {"ok": True}) if fleet.delete_snapshot(parts[1])
                        else self._send(404, {"error": "instantané inconnu"}))
            return self._send(404, {"error": "route inconnue"})
        except Exception as e:                       # noqa: BLE001
            return self._fail500(e)

    # -- parc ----------------------------------------------------------------

    def _list(self):
        cams = fleet.load_cameras()
        idents = fleet.load_identities()
        return self._send(200, {
            "cameras": [_public(c, idents) for c in cams],
            "groups": sorted({(c.get("group") or "").strip()
                              for c in cams if (c.get("group") or "").strip()}),
        })

    def _create(self, body):
        host = (body.get("host") or "").strip()
        if not host:
            return self._send(400, {"error": "adresse (host) requise"})
        kind = body.get("driver") or "panasonic_aw"
        try:
            drivers.get_driver_class(kind)
        except drivers.DriverError as e:
            return self._send(400, {"error": str(e)})
        cam = _apply_fields({
            "id": uuid.uuid4().hex[:12], "driver": kind, "host": host,
            "name": host, "enabled": True,
        }, body)
        cams = fleet.load_cameras()
        clash = _cam_number_conflict(cams, cam["id"], cam.get("cam_number"))
        if clash:
            return self._send(400, {"error": f"le numéro {cam['cam_number']} est déjà "
                                             f"utilisé par « {clash} »"})
        cams.append(cam)
        fleet.save_cameras(cams)
        # Identification opportuniste : si la caméra répond, on connaît son modèle tout de
        # suite (et donc ses capacités). Si elle ne répond pas, l'ajout reste valide.
        try:
            ident = fleet.driver_for(cam).identify()
            fleet.set_identity(cam["id"], ident)
            if ident.get("model") and not cam.get("model"):
                cams = fleet.load_cameras()
                fleet.find(cams, cam["id"])["model"] = ident["model"]
                fleet.save_cameras(cams)
                cam["model"] = ident["model"]
        except Exception:                            # noqa: BLE001
            pass
        return self._send(200, {"ok": True, "camera": _public(cam)})

    def _create_batch(self, body):
        """Ajoute une SÉRIE de caméras à partir d'une adresse de départ et d'un nombre.

        Les caméras d'un plateau sont presque toujours sur des adresses consécutives avec
        les mêmes identifiants ; les saisir une par une est fastidieux et source de fautes.

        Deux garde-fous : on ne crée jamais de doublon d'adresse (une adresse déjà au parc
        est signalée, pas ré-ajoutée), et le compte-rendu est ligne par ligne — sur dix
        caméras, deux qui ne répondent pas doivent se voir."""
        start = (body.get("host") or "").strip()
        try:
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if not start:
            return self._send(400, {"error": "adresse de départ requise"})
        if not 1 <= count <= MAX_BATCH:
            return self._send(400, {"error": f"nombre attendu entre 1 et {MAX_BATCH}"})
        try:
            hosts = _ip_range(start, count)
        except ValueError as e:
            return self._send(400, {"error": str(e)})

        kind = body.get("driver") or "panasonic_aw"
        try:
            drivers.get_driver_class(kind)
        except drivers.DriverError as e:
            return self._send(400, {"error": str(e)})

        cams = fleet.load_cameras()
        existing = {c.get("host") for c in cams}
        prefix = (body.get("name_prefix") or "").strip()
        try:
            first_no = int(body.get("first_number") or 1)
        except (TypeError, ValueError):
            first_no = 1

        created, results = [], []
        for i, host in enumerate(hosts):
            if host in existing:
                results.append({"id": None, "name": host, "ok": False,
                                "error": "déjà au parc", "detail": None})
                continue
            cam = _apply_fields({
                "id": uuid.uuid4().hex[:12], "driver": kind, "host": host, "enabled": True,
            }, dict(body, host=host,
                    name=f"{prefix} {first_no + i}".strip() if prefix else host))
            cams.append(cam)
            created.append(cam)
            existing.add(host)
        fleet.save_cameras(cams)

        # Identification en parallèle : c'est ce qui donne le modèle, donc la famille, donc
        # les bons formats. Une caméra qui ne répond pas reste au parc, marquée en échec.
        def ident(cam):
            info = fleet.driver_for(cam).identify()
            fleet.set_identity(cam["id"], info)
            return info.get("model") or "modèle non renvoyé"

        if created:
            rep = _run_bulk(created, ident)
            saved = fleet.load_cameras()
            for row, cam in zip(rep["results"], created):
                if row and row["ok"]:
                    rec = fleet.find(saved, cam["id"])
                    if rec is not None and not rec.get("model"):
                        rec["model"] = (fleet.load_identities().get(cam["id"]) or {}).get("model", "")
                results.append(row or {"id": cam["id"], "name": cam.get("name"),
                                       "ok": False, "error": "identification impossible",
                                       "detail": None})
            fleet.save_cameras(saved)

        ok = sum(1 for r in results if r.get("ok"))
        return self._send(200, {"ok": True, "results": results,
                                "count_ok": ok, "count_fail": len(results) - ok,
                                "created": len(created)})

    def _update(self, cam_id, body):
        cams = fleet.load_cameras()
        cam = fleet.find(cams, cam_id)
        if not cam:
            return self._send(404, {"error": "caméra inconnue"})
        _apply_fields(cam, body)
        if not cam.get("host"):
            return self._send(400, {"error": "adresse (host) requise"})
        clash = _cam_number_conflict(cams, cam_id, cam.get("cam_number"))
        if clash:
            return self._send(400, {"error": f"le numéro {cam['cam_number']} est déjà "
                                             f"utilisé par « {clash} »"})
        fleet.save_cameras(cams)
        return self._send(200, {"ok": True, "camera": _public(cam)})

    def _delete(self, cam_id):
        cams = fleet.load_cameras()
        if not fleet.find(cams, cam_id):
            return self._send(404, {"error": "caméra inconnue"})
        fleet.save_cameras([c for c in cams if c.get("id") != cam_id])
        fleet.forget(cam_id)
        return self._send(200, {"ok": True})

    # -- actions sur une caméra ----------------------------------------------

    def _cam_action(self, cam, tail, body):
        drv = fleet.driver_for(cam)
        if tail == ["identify"]:
            ident = drv.identify()
            fleet.set_identity(cam["id"], ident)
            if ident.get("model") and not cam.get("model"):
                cams = fleet.load_cameras()
                fleet.find(cams, cam["id"])["model"] = ident["model"]
                fleet.save_cameras(cams)
            return self._send(200, {"ok": True, "identity": ident})
        if tail == ["params"]:
            return self._params_write(cam, drv, body)
        if tail == ["power"]:
            drv.power(bool(body.get("on")))
            return self._send(200, {"ok": True, "on": bool(body.get("on"))})
        if tail == ["console"]:
            return self._send(200, drv.console(body.get("cmd") or "",
                                               body.get("channel") or "ptz"))
        if tail == ["probe"]:
            return self._send(200, {"ok": True, "probe": drv.probe()})
        if tail[:1] == ["presets"] and len(tail) == 2:
            return self._preset_action(cam, drv, tail[1], body)
        if tail == ["network"]:
            return self._camera_network_write(cam, drv, body)
        return self._send(404, {"error": "action inconnue"})

    def _camera_network_write(self, cam, drv, body):
        """Change l'IP (et/ou masque/passerelle/DHCP) d'une caméra. La caméra bascule sur la
        nouvelle adresse : on met à jour le `host` du parc en conséquence, et on OUBLIE
        l'ancienne joignabilité (elle va disparaître le temps que la caméra applique/redémarre)."""
        new_ip = (body.get("ip") or "").strip()
        warning = None
        if new_ip and new_ip != cam.get("host"):
            # Conflit d'adresse : une AUTRE caméra du parc a déjà cette IP.
            #   • si elle est UTILISÉE (numérotée, donc sur la presta) → on REFUSE : il faut
            #     d'abord libérer son adresse, sinon deux caméras actives entreraient en conflit ;
            #   • si elle est au parc mais HORS presta (sans numéro) → on ALERTE mais on autorise.
            other = next((c for c in fleet.load_cameras()
                          if c.get("id") != cam.get("id") and (c.get("host") or "").strip() == new_ip), None)
            if other is not None:
                who = other.get("name") or other.get("host")
                if other.get("cam_number") is not None:
                    return self._send(409, {"error": "adresse %s déjà utilisée par « %s » (n° %s, "
                                            "sur la presta) — changez d'abord SON adresse"
                                            % (new_ip, who, other.get("cam_number"))})
                warning = ("adresse %s déjà présente au parc sur « %s » (hors presta) — "
                           "affectation autorisée mais à surveiller") % (new_ip, who)
        try:
            drv.set_network(ip=new_ip or None, netmask=(body.get("netmask") or None),
                            gateway=(body.get("gateway") or None), dhcp=body.get("dhcp"))
        except drivers.DriverError as e:
            return self._send(502, {"error": str(e)})
        if new_ip and new_ip != cam.get("host"):
            cams = fleet.load_cameras()
            rec = fleet.find(cams, cam["id"])
            if rec is not None:
                rec["host"] = new_ip
                fleet.save_cameras(cams)
            fleet.forget(cam["id"])          # l'état de joignabilité de l'ancienne IP n'a plus de sens
        return self._send(200, {"ok": True, "host": new_ip or cam.get("host"), "warning": warning})

    def _discover(self, body):
        """« Retrouver par série » : balaie les /24 des caméras du parc, interroge chaque
        répondeur (getinfo), et met à jour le `host` de toute caméra dont le NUMÉRO DE SÉRIE
        est retrouvé à une autre adresse. Le parc s'identifie par série, l'IP n'est qu'un
        emplacement courant."""
        cams = fleet.load_cameras()
        idents = fleet.load_identities()
        want = {}                             # serial -> cam_id
        for c in cams:
            s = ((idents.get(c.get("id")) or {}).get("serial") or "").strip()
            if s:
                want[s] = c.get("id")
        if not want:
            return self._send(400, {"error": "aucune caméra n'a de numéro de série relevé "
                                             "— identifiez d'abord les caméras"})
        subnets = set()
        for c in cams:
            parts = (c.get("host") or "").split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                subnets.add(".".join(parts[:3]))
        targets = [f"{net}.{i}" for net in sorted(subnets) for i in range(1, 255)]

        found = {}                            # serial -> ip
        flock = threading.Lock()

        def probe(ip):
            # getinfo UNIQUE (pas les fallbacks QID/QSV d'identify) : un seul délai d'attente
            # par adresse morte, sinon un /24 injoignable prend des minutes.
            try:
                drv = drivers.build({"driver": "panasonic_aw", "host": ip}, fleet.DEFAULTS)
                drv.timeout = 1.0
                txt = drv._get("/cgi-bin/getinfo", {"FILE": "1"})
            except Exception:                 # noqa: BLE001 — un non-répondeur ne compte pas
                return
            for line in txt.splitlines():
                if line.startswith("SERIAL="):
                    serial = line[len("SERIAL="):].strip()
                    if serial:
                        with flock:
                            found.setdefault(serial, ip)
                    return

        queue = list(targets)
        qlock = threading.Lock()

        def worker():
            while True:
                with qlock:
                    if not queue:
                        return
                    ip = queue.pop()
                probe(ip)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Applique : met à jour le host des caméras retrouvées à une nouvelle adresse.
        saved = fleet.load_cameras()
        changes = []
        for serial, cid in want.items():
            ip = found.get(serial)
            rec = fleet.find(saved, cid)
            if rec is None:
                continue
            if ip and ip != rec.get("host"):
                changes.append({"id": cid, "name": rec.get("name"), "serial": serial,
                                "old": rec.get("host"), "new": ip})
                rec["host"] = ip
                fleet.forget(cid)
        if changes:
            fleet.save_cameras(saved)
        return self._send(200, {"ok": True, "scanned": len(targets),
                                "found": len(found), "changes": changes})

    def _params_read(self, cam, query):
        drv = fleet.driver_for(cam)
        keys = [k for k in (query.get("keys", [""])[0] or "").split(",") if k]
        schema = drv.params_schema()
        return self._send(200, {"schema": schema, "outputs": drv.outputs(),
                                "values": drv.read_params(keys or None)})

    def _params_write(self, cam, drv, body):
        """Écriture d'un ou plusieurs paramètres sur UNE caméra. Chaque clé porte son
        propre résultat : écrire le format et échouer sur le gain ne doit pas masquer
        que le format, lui, est bien passé."""
        values = body.get("values") or {}
        if not isinstance(values, dict) or not values:
            return self._send(400, {"error": "aucun paramètre à écrire"})
        report = {}
        for key, val in values.items():
            try:
                drv.write_param(key, val)
                report[key] = {"ok": True, "error": None}
            except drivers.DriverError as e:
                report[key] = {"ok": False, "error": str(e)}
        ok = all(r["ok"] for r in report.values())
        return self._send(200, {"ok": ok, "report": report})

    # -- mémoires ------------------------------------------------------------

    def _presets(self, cam):
        """Grille des mémoires, noms locaux superposés à ceux (éventuels) de la caméra."""
        drv = fleet.driver_for(cam)
        on_device = drivers.CAP_PRESET_NAMES in drv.capabilities()
        local = fleet.names_for(cam["id"])
        rows = []
        for p in drv.presets():
            name_local = local.get(str(p["index"]), "")
            # Quand la caméra STOCKE les noms, c'est elle qui fait autorité : afficher un
            # nom local qui masquerait un nom embarqué différent serait trompeur — l'écran
            # doit montrer ce qui est réellement dans la machine.
            shown = (p.get("name") or name_local) if on_device else (name_local or p.get("name") or "")
            rows.append({
                "index": p["index"],
                "name": shown,
                "device_name": p.get("name") or "",
                "local": bool(name_local),
                "used": p.get("used"),
            })
        lo, hi = type(drv).PRESET_RANGE
        return self._send(200, {"presets": rows, "range": [lo, hi],
                                "names_on_device": drivers.CAP_PRESET_NAMES in drv.capabilities()})

    def _preset_action(self, cam, drv, action, body):
        index = body.get("index")
        if index is None:
            return self._send(400, {"error": "numéro de mémoire requis"})
        if action == "recall":
            drv.recall_preset(index)
            return self._send(200, {"ok": True})
        if action == "store":
            drv.store_preset(index)
            return self._send(200, {"ok": True})
        if action == "name":
            name = body.get("name") or ""
            pushed, effective = False, name
            if drivers.CAP_PRESET_NAMES in drv.capabilities():
                try:
                    # La caméra peut n'accepter qu'une forme adaptée du nom (jeu de
                    # caractères, longueur) : on stocke localement CE QU'ELLE A RETENU,
                    # sinon l'outil afficherait un nom que la machine n'a pas.
                    written = drv.rename_preset(index, name)
                    effective = written if written is not None else name
                    pushed = True
                except drivers.Unsupported:
                    pushed = False          # le modèle ne sait pas : le nom local suffit
            fleet.set_name(cam["id"], index, effective)
            return self._send(200, {"ok": True, "pushed_to_device": pushed,
                                    "name": effective,
                                    "changed": effective != name})
        return self._send(404, {"error": "action de mémoire inconnue"})

    def _snapshot(self, cam):
        data, ctype = fleet.driver_for(cam).snapshot()
        return self._send_raw(data, ctype)

    # -- vue d'ensemble ------------------------------------------------------

    def _overview_one(self, cam):
        """Ligne de vue d'ensemble d'UNE caméra. Le front en appelle une par caméra et
        remplit le tableau au fil des réponses, plutôt que d'attendre tout le parc."""
        drv = _require_reachable(fleet.driver_for(cam))
        outs = drv.outputs()
        keys = ["video_freq"] + [_okey(o["value"]) for o in outs]
        values = drv.read_params(keys)
        if not any(v is not None for v in values.values()):
            raise drivers.DriverError("aucune valeur lisible (caméra injoignable ?)")
        # Le libellé de fréquence est calculé ICI : il dépend du modèle (2 valeurs sur
        # AW-HE130, 5 sur AW-UE160) et c'est le pilote qui fait autorité, pas le front.
        return {"outputs": outs, "values": values,
                "freq_label": _freq_label(cam, values.get("video_freq"))}

    def _names_one(self, cam):
        """Noms de mémoires d'UNE caméra, même principe."""
        drv = fleet.driver_for(cam)
        on_device = drivers.CAP_PRESET_NAMES in drv.capabilities()
        if on_device:
            _require_reachable(drv)
        local = fleet.names_for(cam["id"])
        names = {}
        for p in drv.presets():
            i = p["index"]
            dev, loc = p.get("name") or "", local.get(str(i), "")
            names[str(i)] = (dev or loc) if on_device else (loc or dev)
        lo, hi = type(drv).PRESET_RANGE
        return {"on_device": on_device, "lo": lo, "hi": hi, "names": names}


    def _overview(self):
        """Tableau transverse du parc : une ligne par caméra, une colonne par sortie vidéo.

        Les modèles n'ont pas les mêmes sorties (une HE130 n'a qu'un réglage général, une
        UE160 en a sept). Les colonnes sont donc l'UNION des sorties du parc, et une caméra
        qui n'a pas une sortie laisse la case vide — plutôt que de fabriquer une valeur ou
        de couper l'affichage au plus petit dénominateur."""
        cams = fleet.load_cameras()

        rep = _run_bulk(cams, self._overview_one)
        columns, seen = [], set()
        rows = []
        for row, cam in zip(rep["results"], cams):
            st = fleet.status_of(cam.get("id"))
            data = (row or {}).get("detail") if (row or {}).get("ok") else None
            entry = {
                "id": cam.get("id"), "name": cam.get("name"), "host": cam.get("host"),
                "model": cam.get("model") or "", "reachable": st["reachable"],
                "error": (row or {}).get("error"), "freq": None, "cells": {},
            }
            if data:
                for o in data["outputs"]:
                    if o["value"] not in seen:
                        seen.add(o["value"])
                        columns.append(o)
                    entry["cells"][o["value"]] = data["values"].get(_okey(o["value"]))
                entry["freq"] = data["values"].get("video_freq")
                entry["freq_label"] = _freq_label(cam, entry["freq"])
            rows.append(entry)
        return self._send(200, {"columns": columns, "rows": rows,
                                "count_ok": rep["count_ok"], "count_fail": rep["count_fail"]})

    def _presets_matrix(self):
        """Tous les noms de mémoires du parc, en une fois : lignes = caméras, colonnes =
        numéros de mémoire.

        On renvoie la plage COMPLÈTE de chaque caméra plutôt qu'une tranche : les noms sont
        de courtes chaînes, le coût est dans les allers-retours réseau (déjà parallélisés),
        et l'UI peut alors filtrer « seulement les nommées » sur tout le parc sans relire."""
        cams = fleet.load_cameras()

        rep = _run_bulk(cams, self._names_one)
        rows, lo, hi = [], None, None
        for row, cam in zip(rep["results"], cams):
            data = (row or {}).get("detail") if (row or {}).get("ok") else None
            entry = {"id": cam.get("id"), "name": cam.get("name"),
                     "model": cam.get("model") or "", "error": (row or {}).get("error"),
                     "on_device": False, "names": {}, "lo": None, "hi": None}
            if data:
                entry.update(data)
                lo = data["lo"] if lo is None else min(lo, data["lo"])
                hi = data["hi"] if hi is None else max(hi, data["hi"])
            rows.append(entry)
        return self._send(200, {"cameras": rows, "range": [lo or 1, hi or 100],
                                "count_ok": rep["count_ok"], "count_fail": rep["count_fail"]})

    # -- instantanés ---------------------------------------------------------

    def _snapshot_create(self, body):
        """Capture l'état d'une SÉLECTION de caméras (toutes par défaut)."""
        ids = body.get("ids")
        cams = fleet.load_cameras()
        if ids:
            cams = [c for c in cams if c.get("id") in set(ids)]
        if not cams:
            return self._send(400, {"error": "aucune caméra à relever"})

        rep = _run_bulk(cams, _capture_one)
        entries = []
        for row, cam in zip(rep["results"], cams):
            if row and row["ok"]:
                entries.append(row["detail"])
            else:
                # Une caméra injoignable est CONSERVÉE dans l'instantané, marquée en erreur.
                # L'effacer donnerait un instantané qui prétend couvrir tout le parc alors
                # qu'il en a raté une partie.
                entries.append({"id": cam.get("id"), "name": cam.get("name"),
                                "host": cam.get("host"), "driver": cam.get("driver"),
                                "model": cam.get("model") or "", "values": {}, "schema": [],
                                "error": (row or {}).get("error") or "relevé impossible"})
        snap = {
            "id": uuid.uuid4().hex[:12],
            "name": (body.get("name") or "").strip() or "Instantané",
            "note": (body.get("note") or "").strip(),
            "created_at": time.time(),
            "cameras": entries,
        }
        fleet.save_snapshot(snap)
        return self._send(200, {"ok": True, "snapshot": snap,
                                "count_ok": rep["count_ok"], "count_fail": rep["count_fail"]})

    def _snapshot_compare(self, sid):
        """Compare l'état COURANT du parc à un instantané, paramètre par paramètre.

        La comparaison porte sur tout ce qui a été relevé ; l'inscriptibilité est évaluée
        sur le schéma COURANT de la caméra, pas sur celui figé dans l'instantané — c'est
        lui qui dira ce qu'on sait réellement réécrire aujourd'hui."""
        snap = fleet.get_snapshot(sid)
        if not snap:
            return self._send(404, {"error": "instantané inconnu"})
        live = {c.get("id"): c for c in fleet.load_cameras()}

        def compare_one(entry):
            cam = live.get(entry.get("id"))
            if not cam:
                return {"present": False, "params": [], "error": None}
            drv = fleet.driver_for(cam)
            schema = {p["key"]: p for p in drv.params_schema()}
            current = drv.read_params()
            rows = []
            for p in (entry.get("schema") or []):
                key = p["key"]
                saved, now = entry.get("values", {}).get(key), current.get(key)
                cur_p = schema.get(key)
                if now is None:
                    status = "unread"
                elif saved is None:
                    status = "unsaved"
                else:
                    status = "same" if saved == now else "diff"
                rows.append({
                    "key": key, "label": (cur_p or p).get("label", key),
                    "saved": saved, "current": now, "status": status,
                    # Restaurable = écrivable AUJOURD'HUI et on a bien une valeur à renvoyer.
                    "restorable": bool(cur_p and cur_p.get("writable") and saved is not None),
                    "heavy": bool((cur_p or p).get("heavy")),
                })
            return {"present": True, "params": rows, "error": None}

        entries = snap.get("cameras") or []
        rep = _run_bulk(entries, compare_one)
        out, tally = [], {"same": 0, "diff": 0, "unread": 0, "unsaved": 0,
                          "absent": 0, "restorable": 0}
        for row, entry in zip(rep["results"], entries):
            res = (row or {}).get("detail") if (row or {}).get("ok") else None
            err = (row or {}).get("error") or entry.get("error")
            if res is None:
                res = {"present": bool(err is None), "params": [], "error": err}
            if not res["present"]:
                tally["absent"] += 1
            for p in res["params"]:
                tally[p["status"]] = tally.get(p["status"], 0) + 1
                if p["status"] == "diff" and p["restorable"]:
                    tally["restorable"] += 1
            out.append({"id": entry.get("id"), "name": entry.get("name"),
                        "host": entry.get("host"), "model": entry.get("model"),
                        "present": res["present"], "error": res.get("error"),
                        "params": res["params"]})
        return self._send(200, {
            "snapshot": {k: snap.get(k) for k in ("id", "name", "note", "created_at")},
            "cameras": out, "tally": tally})

    def _snapshot_restore(self, sid, body):
        """Restauration SÉLECTIVE : l'appelant désigne, caméra par caméra, les clés à
        renvoyer. Rien n'est écrit qui n'ait été explicitement demandé."""
        snap = fleet.get_snapshot(sid)
        if not snap:
            return self._send(404, {"error": "instantané inconnu"})
        items = body.get("items") or []
        if not isinstance(items, list) or not items:
            return self._send(400, {"error": "aucun paramètre sélectionné"})
        wanted = {}
        for it in items:
            keys = [k for k in (it.get("keys") or []) if k]
            if keys:
                wanted.setdefault(it.get("camera_id"), set()).update(keys)
        if not wanted:
            return self._send(400, {"error": "aucun paramètre sélectionné"})

        saved = {e.get("id"): e for e in (snap.get("cameras") or [])}
        live = {c.get("id"): c for c in fleet.load_cameras()}
        targets = [live[cid] for cid in wanted if cid in live and cid in saved]
        if not targets:
            return self._send(404, {"error": "aucune caméra restaurable dans la sélection"})

        def restore_one(cam):
            cid = cam.get("id")
            entry = saved[cid]
            drv = fleet.driver_for(cam)
            schema = {p["key"]: p for p in drv.params_schema()}
            # Ordre d'écriture imposé par le pilote (`order`) : chez Panasonic la fréquence
            # vidéo doit précéder le format, sinon la caméra refuse le format envoyé.
            keys = sorted(wanted[cid], key=lambda k: schema.get(k, {}).get("order", 50))
            done, failed = [], []
            for key in keys:
                p, value = schema.get(key), entry.get("values", {}).get(key)
                if not p or not p.get("writable"):
                    failed.append(f"{key} : non inscriptible")
                    continue
                if value is None:
                    failed.append(f"{key} : rien d'enregistré")
                    continue
                try:
                    drv.write_param(key, value)
                    done.append(p["label"])
                except drivers.DriverError as e:
                    failed.append(f"{p['label']} : {e}")
            if failed:
                raise drivers.DriverError(" · ".join(failed)
                                          + (f" (appliqués : {', '.join(done)})" if done else ""))
            return ", ".join(done)

        return self._send(200, _run_bulk(targets, restore_one))

    # -- actions groupées ----------------------------------------------------

    def _bulk(self, action, body):
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return self._send(400, {"error": "aucune caméra sélectionnée"})
        cams = [c for c in fleet.load_cameras() if c.get("id") in set(ids)]
        if not cams:
            return self._send(404, {"error": "aucune caméra correspondante"})

        if action == "params":
            values = body.get("values") or {}
            if not values:
                return self._send(400, {"error": "aucun paramètre à appliquer"})

            def apply_params(cam):
                drv = fleet.driver_for(cam)
                done = []
                for key, val in values.items():
                    drv.write_param(key, val)       # une erreur → toute la ligne échoue
                    done.append(key)
                return ", ".join(done)

            return self._send(200, _run_bulk(cams, apply_params))

        if action == "preset":
            index = body.get("index")
            mode = body.get("mode") or "recall"
            if index is None:
                return self._send(400, {"error": "numéro de mémoire requis"})
            if mode not in ("recall", "store"):
                return self._send(400, {"error": "mode de mémoire invalide"})

            def do_preset(cam):
                drv = fleet.driver_for(cam)
                (drv.store_preset if mode == "store" else drv.recall_preset)(index)
                return f"mémoire {index} ({'enregistrée' if mode == 'store' else 'rappelée'})"

            return self._send(200, _run_bulk(cams, do_preset))

        if action == "name":
            # Renommage de LA MÊME mémoire sur plusieurs caméras (ex. « mémoire 3 =
            # Plateau large » sur les six caméras du plateau). Le nom est POUSSÉ dans les
            # caméras qui savent le stocker et tenu localement pour les autres — sans quoi
            # un même parc afficherait des noms cohérents dans l'outil mais pas sur les
            # machines.
            index, name = body.get("index"), body.get("name") or ""
            if index is None:
                return self._send(400, {"error": "numéro de mémoire requis"})

            def do_name(cam):
                drv = fleet.driver_for(cam)
                effective, pushed = name, False
                if drivers.CAP_PRESET_NAMES in drv.capabilities():
                    try:
                        written = drv.rename_preset(index, name)
                        effective = written if written is not None else name
                        pushed = True
                    except drivers.Unsupported:
                        pushed = False
                fleet.set_name(cam["id"], index, effective)
                return (f"mémoire {index} = « {effective} »"
                        + (" (dans la caméra)" if pushed else " (nom local)"))

            return self._send(200, _run_bulk(cams, do_name))

        if action == "identify":
            def do_identify(cam):
                ident = fleet.driver_for(cam).identify()
                fleet.set_identity(cam["id"], ident)
                return ident.get("model") or "modèle non renvoyé"

            return self._send(200, _run_bulk(cams, do_identify))

        return self._send(404, {"error": "action groupée inconnue"})


    # -- pupitres ------------------------------------------------------------

    # Champs du parc de pupitres modifiables par l'UI (liste blanche).
    PANEL_FIELDS = ("name", "driver", "host", "port", "user", "password",
                    "model", "notes", "enabled", "timeout")

    def _panel_or_404(self, panel_id):
        panel = panel_fleet.get(panel_id)
        if not panel:
            self._send(404, {"error": "pupitre inconnu"})
            return None
        return panel

    def _panel_public(self, panel):
        """Vue d'un pupitre pour le navigateur. Le mot de passe n'en sort JAMAIS."""
        st = panel_fleet.status_of(panel.get("id"))
        caps, slots = [], 0
        driver_ok, driver_err = True, None
        try:
            drv = panel_fleet.driver_for(panel)
            caps = sorted(drv.capabilities())
            slots = type(drv).SLOT_COUNT
        except panels.PanelError as e:
            driver_ok, driver_err = False, str(e)
        return {
            "id": panel.get("id"),
            "name": panel.get("name"),
            "driver": panel.get("driver"),
            "host": panel.get("host"),
            "port": panel.get("port"),
            "user": panel.get("user") or "",
            "has_password": bool(panel.get("password")),
            "model": panel.get("model") or "",
            "notes": panel.get("notes") or "",
            "enabled": panel.get("enabled", True),
            "timeout": panel.get("timeout") or 6,
            "capabilities": caps,
            "slot_count": slots,
            "driver_ok": driver_ok,
            "driver_error": driver_err,
            "reachable": st["reachable"],
            "latency_ms": st["latency_ms"],
            "checked_at": st["checked_at"],
            "error": st["error"],
        }

    def _apply_panel_fields(self, panel, body):
        for f in self.PANEL_FIELDS:
            if f not in body:
                continue
            v = body[f]
            if f == "port":
                panel[f] = int(v) if v else None
            elif f == "enabled":
                panel[f] = bool(v)
            elif f == "timeout":
                panel[f] = max(1, min(30, float(v or 6)))
            elif f == "password":
                if v:                       # vide = « ne change rien » (jamais renvoyé au front)
                    panel[f] = str(v)
            else:
                panel[f] = ("" if v is None else str(v)).strip()
        return panel

    def _panels_list(self):
        items = panel_fleet.load_panels()
        return self._send(200, {"panels": [self._panel_public(p) for p in items]})

    def _panel_create(self, body):
        host = (body.get("host") or "").strip()
        if not host:
            return self._send(400, {"error": "adresse (host) requise"})
        kind = body.get("driver") or "panasonic_rp"
        try:
            panels.get_panel_class(kind)
        except panels.PanelError as e:
            return self._send(400, {"error": str(e)})
        panel = self._apply_panel_fields({
            "id": uuid.uuid4().hex[:12], "driver": kind, "host": host,
            "name": host, "enabled": True,
        }, body)
        items = panel_fleet.load_panels()
        items.append(panel)
        panel_fleet.save_panels(items)
        return self._send(200, {"ok": True, "panel": self._panel_public(panel)})

    def _panel_update(self, panel_id, body):
        items = panel_fleet.load_panels()
        panel = panel_fleet.find(items, panel_id)
        if not panel:
            return self._send(404, {"error": "pupitre inconnu"})
        self._apply_panel_fields(panel, body)
        if not panel.get("host"):
            return self._send(400, {"error": "adresse (host) requise"})
        panel_fleet.save_panels(items)
        return self._send(200, {"ok": True, "panel": self._panel_public(panel)})

    def _panel_delete(self, panel_id):
        items = panel_fleet.load_panels()
        if not panel_fleet.find(items, panel_id):
            return self._send(404, {"error": "pupitre inconnu"})
        panel_fleet.save_panels([p for p in items if p.get("id") != panel_id])
        panel_fleet.forget(panel_id)
        return self._send(200, {"ok": True})

    def _panel_identify(self, panel):
        drv = panel_fleet.driver_for(panel)
        return self._send(200, {"ok": True, "identity": drv.identify()})

    def _panel_assignments(self, panel):
        """Table d'affectation courante, chaque slot recoupé avec le parc caméras : si une
        IP affectée correspond à une caméra connue, on joint son nom. Le parc reste la
        source des libellés — le pupitre, lui, ne connaît que des adresses."""
        drv = panel_fleet.driver_for(panel)
        rows = drv.read_assignments()
        cams = [{"id": c.get("id"), "name": c.get("name") or c.get("host"),
                 "host": (c.get("host") or "").strip(), "cam_number": c.get("cam_number")}
                for c in fleet.load_cameras() if (c.get("host") or "").strip()]
        by_host = {c["host"]: c for c in cams}
        for r in rows:
            r["camera"] = by_host.get((r.get("ip") or "").strip())   # None si hors parc
        return self._send(200, {"assignments": rows, "cameras": cams,
                                "slot_count": type(drv).SLOT_COUNT})

    def _resolve_camera_row(self, row):
        """Une ligne peut désigner une caméra du parc par `camera_id` plutôt que de répéter
        son adresse. Le serveur résout alors IP/port/user/MOT DE PASSE depuis le parc — le
        mot de passe reste ainsi côté serveur, jamais exposé au navigateur. Renvoie la ligne
        prête pour le pilote, ou lève ValueError si la caméra est inconnue."""
        cam = fleet.get(row["camera_id"])
        if not cam:
            raise ValueError(f"caméra inconnue : {row['camera_id']}")
        return {
            "slot": row["slot"],
            "control_type": panels.CTRL_LAN,
            "ip": (cam.get("host") or "").strip(),
            "port": cam.get("port") or 80,
            "user": cam.get("user") or fleet.DEFAULTS.get("user") or "",
            "password": cam.get("password") or fleet.DEFAULTS.get("password") or "",
        }

    def _panel_assign_write(self, panel, body):
        """Écrit une ou plusieurs affectations. `body.rows` = liste partielle de slots à
        changer ; le pilote réécrit la table entière en préservant le reste (mots de passe
        compris). C'est une modification de CONFIG D'EXPLOITATION : l'UI la fait confirmer.

        Chaque ligne est soit une caméra du parc (`camera_id` → résolue côté serveur), soit
        une saisie explicite (`control_type`/`ip`/`port`/`user`), soit un simple NoAssign
        (`control_type` = 3)."""
        rows = body.get("rows")
        if not isinstance(rows, list) or not rows:
            return self._send(400, {"error": "aucune affectation à écrire"})
        prepared = []
        for r in rows:
            if not isinstance(r, dict) or "slot" not in r:
                return self._send(400, {"error": "chaque ligne doit porter un « slot »"})
            if r.get("camera_id"):
                try:
                    prepared.append(self._resolve_camera_row(r))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            else:
                prepared.append(r)
        drv = panel_fleet.driver_for(panel)
        drv.write_assignments(prepared)
        # On relit derrière pour que l'UI affiche ce qui est RÉELLEMENT dans le pupitre,
        # jamais la saisie supposée appliquée (même principe que le renommage des mémoires).
        return self._panel_assignments(panel)

    def _macro_read(self, panel, index):
        drv = panel_fleet.driver_for(panel)
        data = drv.read_macro(index)
        # Le n° de caméra d'une étape (slot pupitre) est recoupé avec le parc VIA la table
        # d'affectation courante — pas via le « numéro de caméra », qui peut ne pas coïncider
        # (plusieurs cadreurs). On joint donc slot → IP (affectation) → caméra (parc).
        by_slot = {}
        try:
            for a in drv.read_assignments():
                by_slot[str(a["slot"])] = a.get("ip") or ""
        except panels.PanelError:
            by_slot = {}
        cams_by_host = {}
        for c in fleet.load_cameras():
            h = (c.get("host") or "").strip()
            if h:
                cams_by_host.setdefault(h, {"id": c.get("id"),
                                            "name": c.get("name") or h,
                                            "cam_number": c.get("cam_number")})
        for s in data.get("steps", []):
            ip = by_slot.get(str(s.get("cam")))
            s["camera"] = cams_by_host.get(ip) if ip else None
        data["max_steps"] = type(drv).MACRO_MAX_STEPS
        data["macro_count"] = type(drv).MACRO_COUNT
        return self._send(200, data)

    def _macro_write(self, panel, index, body):
        steps = body.get("steps")
        if not isinstance(steps, list):
            return self._send(400, {"error": "liste d'étapes attendue"})
        drv = panel_fleet.driver_for(panel)
        drv.write_macro(index, steps)
        return self._macro_read(panel, index)

    def _macro_control(self, panel, index, body):
        drv = panel_fleet.driver_for(panel)
        drv.macro_control(index, bool(body.get("play")))
        return self._send(200, {"ok": True, "playing": bool(body.get("play"))})

    # -- contribution Ember+ (consommée par services/emberplus) --------------
    # SANS I/O (cf. bloc Ember+ en tête). Deux volets :
    #   • groupe « Caméras » : un nœud par caméra pour le PILOTAGE (marche/format/mémoire) ;
    #   • UNE matrice (points de croisement, façon VSM). Voir _ember_matrix() pour la
    #     disposition exacte (sources : Disconnect + caméras + numéros ; cibles : numéros +
    #     boutons). Écritures par `ember/connect` (ref {m:"grid"}).
    _SRC_DISCONNECT = 1              # source réservée « Disconnect »

    def _ember_tree(self):
        idents = fleet.load_identities()
        cams = fleet.load_cameras()
        # -- groupe pilotage caméra --
        cam_nodes = []
        for idx, cam in enumerate(cams, start=1):
            cid = cam.get("id")
            ev = _ember_get(cid)
            st = fleet.status_of(cid)
            params = [{"id": 1, "label": "Numéro", "type": "string",
                       "value": ("" if cam.get("cam_number") is None else str(cam["cam_number"])),
                       "writable": False},
                      {"id": 2, "label": "Joignable", "type": "bool",
                       "value": bool(st.get("reachable")), "writable": False}]
            model = (idents.get(cid) or {}).get("model") or cam.get("model") or ""
            if model:
                params.append({"id": 3, "label": "Modèle", "type": "string",
                               "value": model, "writable": False})
            if "power" in ev:
                params.append({"id": 10, "label": "Marche", "type": "bool",
                               "value": bool(ev.get("power")), "writable": True,
                               "ref": {"cam": cid, "field": "power"}})
            opts = ev.get("format_options") or []
            if opts:
                cur = ev.get("format")
                params.append({"id": 11, "label": "Format vidéo", "type": "enum",
                               "value": cur if cur in opts else opts[0], "writable": True,
                               "enum": [{"value": f, "label": f} for f in opts],
                               "ref": {"cam": cid, "field": "format"}})
            sub = []
            pr = ev.get("preset_range")
            if pr:
                enum = [{"value": 0, "label": "—"}] + [
                    {"value": i, "label": "Mémoire %d" % i} for i in range(pr[0], pr[1] + 1)]
                sub.append({"id": 100, "label": "Mémoires", "params": [
                    {"id": 1, "label": "Rappeler", "type": "enum", "value": 0, "writable": True,
                     "enum": enum, "ref": {"cam": cid, "field": "preset"}}]})
            cam_nodes.append({"id": idx, "label": cam.get("name") or cam.get("host"),
                              "desc": cam.get("host") or "", "params": params, "nodes": sub})

        return self._send(200, {"label": "Caméras tourelles", "nodes": [
            {"id": 1, "label": "Caméras", "nodes": cam_nodes},
            self._ember_matrix(cams),
        ]})

    def _ember_matrix(self, cams):
        """LA matrice unique.
        SOURCES (lignes) : 1 = Disconnect ; 2..(1+K) = caméras renseignées (→ numérotation) ;
        puis, à partir d'un offset arrondi (→ 101/151/201…), les NUMÉROS 1..GRID_CAMERAS
        (→ affectation des boutons). CIBLES (colonnes) : 1..GRID_CAMERAS = numéros ; puis
        GRID_BUTTONS cibles par pupitre. Seuls deux croisements ont un sens : caméra×numéro
        (numérote) et numéro×bouton (affecte) ; le `connect` refuse les autres."""
        offset = _ember_num_offset(len(cams))
        # sources caméra (2..) + map n° source → cam_id (figée pour le connect)
        cam_sorted = sorted(cams, key=lambda c: (c.get("cam_number") is None,
                                                 c.get("cam_number") or 0, c.get("name") or ""))
        sources = [{"number": self._SRC_DISCONNECT, "label": "⏻ Disconnect"}]
        camsrc, cam_by_id = {}, {}
        for i, c in enumerate(cam_sorted, start=2):
            num = c.get("cam_number")
            label = (("N°%s · " % num) if num is not None else "") + (c.get("name") or c.get("host"))
            sources.append({"number": i, "label": label})
            camsrc[i] = c.get("id")
            cam_by_id[c.get("id")] = i
        with _ember_lock:
            _ember_matrix_meta["offset"] = offset
            _ember_matrix_meta["camsrc"] = dict(camsrc)
        # sources numéro (offset+1 .. offset+GRID_CAMERAS)
        num_owner = {c.get("cam_number"): c for c in cams if c.get("cam_number")}
        for n in range(1, GRID_CAMERAS + 1):
            owner = num_owner.get(n)
            lbl = ("N°%d" % n) + (" · %s" % (owner.get("name") or owner.get("host")) if owner else "")
            sources.append({"number": offset + n, "label": lbl})

        # cibles : numéros (1..GRID_CAMERAS) puis boutons ; connexions (tally)
        targets, connections = [], []
        for n in range(1, GRID_CAMERAS + 1):
            targets.append({"number": n, "label": "N°%d" % n})
            owner = num_owner.get(n)
            if owner is not None:                    # cible numéro n ← source caméra qui la porte
                connections.append({"target": n, "sources": [cam_by_id[owner.get("id")]]})
        for pi, panel in enumerate(panel_fleet.load_panels()[:GRID_PANELS]):
            pname = panel.get("name") or panel.get("host")
            btns = panel_fleet.get_buttons(panel.get("id"))
            for b in range(1, GRID_BUTTONS + 1):
                t = _ember_panel_target(pi, b)
                targets.append({"number": t, "label": "%s · B%d" % (pname, b)})
                num = btns.get(b)                    # cible bouton ← source NUMÉRO qui y est posé
                if num:
                    connections.append({"target": t, "sources": [offset + num]})

        return {"id": 900, "label": "Grille caméras", "matrix": {
            "type": "oneToN",
            "description": "Numérotation (caméra×n°) + pupitres (n°×bouton)",
            "targets": targets, "sources": sources, "connections": connections,
            "ref": {"m": "grid"}}}

    def _ember_set(self, body):
        """Écritures des PARAMÈTRES caméra (marche/format/mémoire). La grille (numéros +
        boutons) passe, elle, par `ember/connect` (matrice)."""
        ref = body.get("ref") or {}
        val = body.get("value")
        cam = fleet.get(ref.get("cam"))
        if not cam:
            return self._send(404, {"error": "caméra inconnue"})
        drv = fleet.driver_for(cam)
        field = ref.get("field")
        if field == "power":
            drv.power(bool(val))
        elif field == "format":
            drv.write_param("video_format", val)
        elif field == "preset":
            n = int(val or 0)
            if n > 0:
                drv.recall_preset(n)
        else:
            return self._send(400, {"error": "champ Ember+ inconnu"})
        _ember_refresh_one(cam)      # relit pour refléter le nouvel état au prochain balayage
        return self._send(200, {"ok": True})

    def _ember_connect(self, body):
        """Croisement de la matrice « grille » (VSM → outil). oneToN.
        Cible NUMÉRO (1..GRID_CAMERAS) : la source doit être une CAMÉRA → on lui donne ce
        numéro (l'ancien porteur le perd), puis on PROPAGE aux boutons portant les numéros
        touchés. Cible BOUTON : la source doit être un NUMÉRO → le bouton prend ce numéro
        (stocké) et on résout numéro→caméra→IP. Source 1 (ou disconnect/vide) = effacer."""
        ref = body.get("ref") or {}
        if ref.get("m") != "grid":
            return self._send(400, {"error": "matrice inconnue"})
        try:
            target = int(body.get("target"))
        except (TypeError, ValueError):
            return self._send(400, {"error": "cible invalide"})
        op = (body.get("operation") or "absolute").lower()
        srcs = body.get("sources") or []
        src = int(srcs[0]) if srcs else 0
        disconnect = op == "disconnect" or not srcs or src == self._SRC_DISCONNECT
        with _ember_lock:
            offset = _ember_matrix_meta["offset"]
            camsrc = dict(_ember_matrix_meta["camsrc"])
        kind = _ember_decode_target(target)

        if kind[0] == "number":
            n = kind[1]
            cams = fleet.load_cameras()
            affected = {n}
            for c in cams:                               # oneToN : purge l'ancien porteur de n
                if c.get("cam_number") == n:
                    c["cam_number"] = None
            if not disconnect:
                cam_id = camsrc.get(src)
                if cam_id is None:
                    return self._send(400, {"error": "sur une colonne « numéro », la source "
                                                     "doit être une caméra"})
                rec = fleet.find(cams, cam_id)
                if rec is not None:
                    if rec.get("cam_number"):
                        affected.add(rec["cam_number"])  # ancien numéro de cette caméra, libéré
                    rec["cam_number"] = n
            fleet.save_cameras(cams)
            _ember_propagate(affected)                   # les boutons suivent la renumérotation
            return self._send(200, {"ok": True, "target": target,
                                    "sources": [] if disconnect else [src]})

        if kind[0] == "button":
            pi, b = kind[1], kind[2]
            plist = panel_fleet.load_panels()[:GRID_PANELS]
            if pi >= len(plist):
                return self._send(404, {"error": "pupitre inconnu"})
            panel = plist[pi]
            if disconnect:
                panel_fleet.set_button(panel.get("id"), b, None)
                _ember_push_button(panel, b, None)
            else:
                number = src - offset
                if not 1 <= number <= GRID_CAMERAS:
                    return self._send(400, {"error": "sur une colonne « bouton », la source "
                                                     "doit être un numéro de caméra"})
                panel_fleet.set_button(panel.get("id"), b, number)
                _ember_push_button(panel, b, number)
            return self._send(200, {"ok": True, "target": target,
                                    "sources": [] if disconnect else [src]})
        return self._send(404, {"error": "cible hors grille"})


def main():
    os.makedirs(fleet.DATA_DIR, exist_ok=True)
    fleet.start_poller()
    panel_fleet.start_poller()
    _ember_start_poller()
    print(f"ptz : prêt sur :{PORT} (data={fleet.DATA_DIR}, "
          f"surveillance={fleet.POLL_INTERVAL}s, pilotes={[d['kind'] for d in drivers.catalog()]}, "
          f"pupitres={[d['kind'] for d in panels.catalog()]})",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
