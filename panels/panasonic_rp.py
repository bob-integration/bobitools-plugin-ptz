# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Pilote Panasonic AW-RP — pupitres de télécommande caméra sur CGI HTTP.

Couvre la gamme des pupitres IP Panasonic (AW-RP200, RP150, RP60, RP120, RP50…). Le RP200
est le modèle de référence, RÉTRO-INGÉNIERÉ EN DIRECT le 2026-07-30 sur un exemplaire réel
(firmware inconnu, `admin`/mot de passe web). Aucune documentation publique n'a servi : la
méthode est celle qui a marché pour les caméras — lire le JavaScript que le pupitre sert
lui-même (`js/pc/setting_connect_assign.js`) et confronter à la machine.

## Ce qui est vérifié (lecture) et ce qui ne l'est pas (écriture)

- **LECTURE confirmée sur RP200 réel** : `GET /cgi-bin/get_cam_assign_edit` renvoie du
  texte brut `clé=valeur`, une ligne par champ, CINQ champs par slot × **200 slots** :

      control_type_1=1        # 1=LAN, 2=Série, 3=NoAssign
      ipv4_addr_1=10.10.11.1
      port_1=80
      user_1=admin
      pass_1=12345            # le mot de passe caméra ressort EN CLAIR

- **ÉCRITURE confirmée sur RP200 réel** (2026-07-30, aller-retour réversible sur un slot
  libre) : `POST /cgi-bin/set_cam_assign_edit`, form-urlencoded, la table ENTIÈRE des 200
  slots aux mêmes clés `..._N`. Le pupitre réécrit tout d'un bloc ; il n'existe pas
  d'écriture unitaire. Modifier un slot = lire tout, changer le slot, réécrire tout. Succès
  = **204 No Content**.

## Authentification et anti-CSRF

Digest MD5, realm « Control » (≠ Basic des caméras). `requests.HTTPDigestAuth` le gère.

Le CGI d'ÉCRITURE exige en plus un en-tête **`Referer` vers une page `/admin/…`**, et il
en fait un **match strict** : le port par défaut doit être OMIS (`http://host/…`, jamais
`http://host:80/…`, sinon 400). C'est ce qui faisait échouer toute écriture au départ. La
lecture, elle, passe sans Referer.

## Le mot de passe ne remonte jamais au navigateur

`read_assignments()` ne publie qu'un booléen `has_password`. Mais l'écriture DOIT renvoyer
les mots de passe des slots qu'on ne touche pas, sinon on les effacerait. Le pilote relit
donc la table brute (`_read_raw`, mots de passe compris) au moment d'écrire, et ne remplace
que ce que l'appelant fournit explicitement.
"""
import re
import time

import requests
from requests.auth import HTTPDigestAuth

from .base import (
    CAP_ASSIGN, CAP_IDENTIFY, CAP_MACROS, CAP_USER_BUTTONS,
    CTRL_LAN, CTRL_NONE, CTRL_SERIAL,
    MACRO_AW_RAW, MACRO_CGI_GET, MACRO_CGI_POST, MACRO_EMPTY, MACRO_RECALL_MACRO,
    MACRO_RECALL_PRESET, MACRO_WAIT_USER,
    PanelDriver, PanelError, Unsupported, assignment, macro_step, register,
)

# Modèles connus de la gamme. Le protocole d'affectation est commun aux pupitres IP ; seul
# le RP200 est confronté au matériel pour l'instant, les autres restent à valider.
MODELS = [
    ("", "Auto"),
    ("AW-RP200", "AW-RP200"),
    ("AW-RP150", "AW-RP150"),
    ("AW-RP120", "AW-RP120"),
    ("AW-RP60", "AW-RP60"),
    ("AW-RP50", "AW-RP50"),
]

_VALID_CTRL = {CTRL_LAN, CTRL_SERIAL, CTRL_NONE}


@register
class PanasonicRpDriver(PanelDriver):
    KIND = "panasonic_rp"
    LABEL = "Panasonic (pupitre AW-RP)"
    BRAND = "Panasonic"
    AVAILABLE = True
    DEFAULT_PORT = 80
    NEEDS_AUTH = True
    SLOT_COUNT = 200
    MACRO_COUNT = 100
    MACRO_MAX_STEPS = 500
    NOTE = ("Lecture et écriture des affectations validées sur AW-RP200 réel. "
            "Autres modèles de la gamme (RP150/120/60/50) : même protocole présumé, "
            "non confronté.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._session = requests.Session()

    @classmethod
    def models(cls):
        return [{"value": v, "label": lbl} for v, lbl in MODELS]

    # -- transport -----------------------------------------------------------

    def _url(self, path):
        return f"http://{self.host}:{self.port}{path}"

    def _auth(self):
        # Le RP200 impose du Digest ; on ne tente pas Basic (contrairement aux caméras, dont
        # le mode d'accès varie). Un pupitre sans mot de passe reste géré : Digest avec un
        # secret vide échoue proprement en 401, jamais en exception.
        return HTTPDigestAuth(self.user, self.password)

    def _headers(self):
        # Le CGI d'ÉCRITURE (`set_cam_assign_edit`) exige un `Referer` pointant vers une page
        # `/admin/…` — protection anti-CSRF vérifiée sur RP200 réel : sans lui, il répond 400
        # (avec, 204). La lecture, elle, passe sans ; mais l'envoyer partout est inoffensif.
        #
        # PIÈGE : le pupitre fait un match STRICT du Referer. Le port par défaut doit être
        # OMIS (`http://host/…`, pas `http://host:80/…`) — exactement comme le fait un
        # navigateur —, sinon il refuse (400 sur `:80`, 204 sans). On ne met donc le port que
        # s'il n'est pas le port HTTP standard.
        host = self.host if self.port == 80 else f"{self.host}:{self.port}"
        return {"Referer": f"http://{host}/admin/index.html"}

    # Le CGI embarqué du RP200 a des HOQUETS sous charge (5xx ponctuel, connexion coupée en
    # plein Digest). Un réessai bref suffit presque toujours. Les écritures réécrivent la
    # TABLE ENTIÈRE (affectations, macros) → idempotentes → réessai sans risque. Et toute
    # exception inattendue est convertie en `PanelError` : le pupitre remonte alors une 502
    # « injoignable » propre, jamais une 500 non gérée.
    _RETRIES = 2

    def _request(self, method, path, data=None, retries=None):
        retries = self._RETRIES if retries is None else retries
        last = None
        for attempt in range(retries + 1):
            try:
                if method == "POST":
                    r = self._session.post(self._url(path), data=data, auth=self._auth(),
                                           headers=self._headers(), timeout=self.timeout)
                else:
                    r = self._session.get(self._url(path), auth=self._auth(),
                                          headers=self._headers(), timeout=self.timeout)
                # Codes DÉTERMINISTES : inutile de réessayer, on remonte tout de suite.
                if r.status_code == 401:
                    raise PanelError("authentification refusée (identifiants ou mode d'accès)")
                if r.status_code == 404:
                    raise PanelError(f"endpoint absent ({path}) — modèle/firmware non pris en charge")
                if r.status_code == 400:
                    raise PanelError("le pupitre a refusé les paramètres (400 Bad Request) — "
                                     "en-tête Referer manquant ou données invalides")
                # 5xx = hoquet serveur embarqué → transitoire, on réessaie.
                if r.status_code >= 500:
                    last = f"réponse {r.status_code} du pupitre (serveur embarqué)"
                elif not r.ok:
                    raise PanelError(f"réponse {r.status_code} du pupitre sur {path}")
                else:
                    return r.text
            except PanelError:
                raise
            except requests.RequestException as e:
                last = f"{self.host} injoignable : {e}"
            except Exception as e:                        # noqa: BLE001 — jamais de 500 non géré
                last = f"erreur inattendue vers le pupitre : {e}"
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))           # court répit avant nouvel essai
        raise PanelError(last or "échec de la requête au pupitre")

    def _get(self, path):
        return self._request("GET", path)

    def _post_form(self, path, data):
        return self._request("POST", path, data=data)

    @staticmethod
    def _parse_kv(text):
        """`clé=valeur` par ligne → dict, en préservant les valeurs vides et les « = » du
        mot de passe (split sur le PREMIER « = » seulement)."""
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v
        return out

    # -- identité & disponibilité --------------------------------------------

    def reachable(self):
        """Léger : un GET de la table d'affectation prouve à la fois que l'hôte répond, que
        l'auth passe, et que c'est bien un pupitre (l'endpoint lui est propre)."""
        self.last_error = None
        try:
            # Sondage : UN seul essai. Inutile de réessayer un pupitre qui ne répond pas —
            # le prochain tour de surveillance (20 s) rattrapera un hoquet passager, sans
            # rallonger chaque sondage de plusieurs délais d'attente.
            self._request("GET", "/cgi-bin/get_cam_assign_edit", retries=0)
            return True
        except PanelError as e:
            self.last_error = str(e)
            return False

    def capabilities(self):
        return {CAP_ASSIGN, CAP_USER_BUTTONS, CAP_IDENTIFY, CAP_MACROS}

    def identify(self):
        # Les endpoints d'identité des caméras (model_serial, modelname, get_basic) sont
        # absents sur le pupitre (404). L'identité fiable dont on dispose est le modèle saisi.
        return {"model": self.model or "AW-RP (Panasonic)"}

    # -- affectation des caméras ---------------------------------------------

    def _read_raw(self):
        """Table brute, MOTS DE PASSE COMPRIS (usage interne : écriture préservante).
        Renvoie une liste ordonnée de dicts {slot, control_type, ip, port, user, password}."""
        kv = self._parse_kv(self._get("/cgi-bin/get_cam_assign_edit"))
        rows, n = [], 1
        while f"control_type_{n}" in kv:
            rows.append({
                "slot": n,
                "control_type": kv.get(f"control_type_{n}", CTRL_NONE),
                "ip": kv.get(f"ipv4_addr_{n}", ""),
                "port": kv.get(f"port_{n}", ""),
                "user": kv.get(f"user_{n}", ""),
                "password": kv.get(f"pass_{n}", ""),
            })
            n += 1
        if not rows:
            raise PanelError("table d'affectation vide ou format inattendu")
        return rows

    def read_assignments(self):
        """Table publique : mot de passe masqué (seul `has_password`)."""
        return [
            assignment(
                slot=r["slot"], control_type=r["control_type"], ip=r["ip"],
                port=r["port"], user=r["user"], has_password=bool(r["password"]),
            )
            for r in self._read_raw()
        ]

    @staticmethod
    def _encode(rows):
        """Table interne → dict de paramètres form-urlencoded pour set_cam_assign_edit."""
        data = {}
        for r in rows:
            n = r["slot"]
            data[f"control_type_{n}"] = r["control_type"]
            data[f"ipv4_addr_{n}"] = r["ip"]
            data[f"port_{n}"] = r["port"]
            data[f"user_{n}"] = r["user"]
            data[f"pass_{n}"] = r["password"]
        return data

    def write_assignments(self, rows):
        """Écrit la table ENTIÈRE. Fusionne sur la table brute courante : un slot absent de
        `rows` est CONSERVÉ tel quel, un mot de passe None/absent est CONSERVÉ. On ne peut
        donc jamais effacer un secret par omission."""
        current = {r["slot"]: r for r in self._read_raw()}
        for row in rows:
            slot = int(row["slot"])
            base = current.get(slot)
            if base is None:
                raise PanelError(f"slot {slot} hors de la plage du pupitre")
            ct = str(row.get("control_type", base["control_type"]))
            if ct not in _VALID_CTRL:
                raise PanelError(f"type de contrôle invalide pour le slot {slot} : {ct}")
            base["control_type"] = ct
            if "ip" in row and row["ip"] is not None:
                base["ip"] = row["ip"]
            if "port" in row and row["port"] is not None:
                base["port"] = str(row["port"])
            if "user" in row and row["user"] is not None:
                base["user"] = row["user"]
            if row.get("password") is not None:
                base["password"] = row["password"]
        ordered = [current[k] for k in sorted(current)]
        self._post_form("/cgi-bin/set_cam_assign_edit", self._encode(ordered))
        return True

    def assign(self, slot, ip=None, port=None, control_type=None, user=None, password=None):
        """Modifie UN slot (lecture-modification-réécriture de la table entière)."""
        row = {"slot": int(slot)}
        if control_type is not None:
            row["control_type"] = str(control_type)
        if ip is not None:
            row["ip"] = ip
        if port is not None:
            row["port"] = str(port)
        if user is not None:
            row["user"] = user
        if password is not None:
            row["password"] = password
        return self.write_assignments([row])

    def clear_slot(self, slot):
        """Repasse un slot en NoAssign sans toucher au reste."""
        return self.assign(slot, control_type=CTRL_NONE)

    # -- macros --------------------------------------------------------------
    #
    # Une macro (1..100) est une suite d'étapes (jusqu'à 500). Sur le fil, chaque étape a
    # quatre champs : `cam_i` (n° de caméra AU PUPITRE), `cmd_i` (commande NATIVE), `interval_i`
    # (délai ms) et `format_i` (0=commande AW, 1=CGI GET, 2=CGI POST). L'UI, elle, manipule des
    # types SÉMANTIQUES (`MACRO_*`) ; le pilote traduit dans les deux sens. Toute commande
    # qu'on ne sait pas décoder reste rejouable telle quelle (type `aw_raw`) : un aller-retour
    # ne dénature JAMAIS une macro existante. Formats relevés sur RP200 réel (cf. en-tête).

    # Un préfixe préset : `#R` + n° de mémoire sur 2 chiffres, DÉCALÉ de 1 (mémoire 1 → #R00),
    # exactement comme le rappel de mémoire des caméras.
    _RE_PRESET = re.compile(r"^#R(\d+)$")

    @staticmethod
    def _decode_cmd(cmd, fmt):
        """Commande native (cmd, format) → (type sémantique, param)."""
        cmd = cmd or ""
        fmt = str(fmt or "0")
        if not cmd:
            return MACRO_EMPTY, ""
        if fmt == "1":
            return MACRO_CGI_GET, cmd
        if fmt == "2":
            return MACRO_CGI_POST, cmd
        if cmd == "WAIT_USER_TRIGGER":
            return MACRO_WAIT_USER, ""
        if cmd.startswith("RECALL_MACRO:"):
            try:
                return MACRO_RECALL_MACRO, str(int(cmd.split(":", 1)[1]))
            except ValueError:
                return MACRO_AW_RAW, cmd
        m = PanasonicRpDriver._RE_PRESET.match(cmd)
        if m:
            return MACRO_RECALL_PRESET, str(int(m.group(1)) + 1)
        return MACRO_AW_RAW, cmd

    def _encode_step(self, step):
        """Étape sémantique {type, param} → (cmd natif, format). Lève `PanelError` sur une
        valeur hors bornes, AVANT l'envoi, pour un message clair plutôt qu'un 400 du pupitre."""
        t = step.get("type") or MACRO_EMPTY
        param = str(step.get("param") if step.get("param") is not None else "").strip()
        if t == MACRO_EMPTY:
            return "", "0"
        if t == MACRO_RECALL_PRESET:
            n = int(param) if param.isdigit() else 0
            if not 1 <= n <= 100:
                raise PanelError("mémoire hors bornes (1..100)")
            return "#R%02d" % (n - 1), "0"
        if t == MACRO_RECALL_MACRO:
            n = int(param) if param.isdigit() else 0
            if not 1 <= n <= self.MACRO_COUNT:
                raise PanelError("macro hors bornes (1..%d)" % self.MACRO_COUNT)
            return "RECALL_MACRO:%03d" % n, "0"
        if t == MACRO_WAIT_USER:
            return "WAIT_USER_TRIGGER", "0"
        if t == MACRO_AW_RAW:
            return param, "0"
        if t == MACRO_CGI_GET:
            return param, "1"
        if t == MACRO_CGI_POST:
            return param, "2"
        raise PanelError("type d'étape inconnu : %s" % t)

    # Types dont l'étape n'a PAS besoin de caméra ni de paramètre (cf. contrôle du JS du
    # pupitre) ; tous les autres exigent une caméra, sinon le CGI répond 400.
    _NO_CAM = {MACRO_WAIT_USER, MACRO_RECALL_MACRO, MACRO_EMPTY}

    def read_macro(self, index):
        index = int(index)
        txt = self._get("/cgi-bin/get_macro_step?select=%d" % index)
        kv = self._parse_kv(txt)
        steps, n = [], 1
        while ("cmd_%d" % n) in kv or ("cam_%d" % n) in kv:
            cmd = kv.get("cmd_%d" % n, "")
            typ, param = self._decode_cmd(cmd, kv.get("format_%d" % n, "0"))
            interval = kv.get("interval_%d" % n, "") or "0"
            steps.append(macro_step(
                index=n, type_=typ, cam=kv.get("cam_%d" % n, ""), param=param,
                interval=int(interval) if str(interval).isdigit() else 0, raw=cmd))
            n += 1
        return {"index": index, "label": "Macro %d" % index, "steps": steps}

    def write_macro(self, index, steps):
        index = int(index)
        steps = steps or []
        if len(steps) > self.MACRO_MAX_STEPS:
            raise PanelError("trop d'étapes (max %d)" % self.MACRO_MAX_STEPS)
        # Règle du firmware, confirmée sur RP200 : une macro ne peut pas SE TERMINER par une
        # attente utilisateur (rien ne viendrait la relancer). Le pupitre le refuse par un 400
        # opaque — on donne ici un message clair, avant l'envoi. (Le pupitre applique d'autres
        # contraintes de séquence qu'on ne réplique pas : elles ressortent en 400 explicite.)
        defined = [s for s in steps if (s or {}).get("type") not in (None, MACRO_EMPTY)]
        if defined and defined[-1].get("type") == MACRO_WAIT_USER:
            raise PanelError("une macro ne peut pas se terminer par une attente utilisateur "
                             "(Wait User Trigger)")
        data = {"select": str(index)}
        # Le pupitre réécrit la macro ENTIÈRE : on envoie toujours les 500 emplacements, vides
        # au-delà des étapes définies (comportement de l'interface native).
        for i in range(1, self.MACRO_MAX_STEPS + 1):
            step = steps[i - 1] if i - 1 < len(steps) else None
            t = (step or {}).get("type") or MACRO_EMPTY
            if step is None or t == MACRO_EMPTY:
                cam = cmd = interval = fmt = ""
            else:
                cmd, fmt = self._encode_step(step)
                cam = step.get("cam")
                cam = "" if cam in (None, "") else str(cam)
                if t not in self._NO_CAM and not cam:
                    raise PanelError("étape %d : une caméra est requise pour ce type" % i)
                iv = step.get("interval")
                interval = "0" if iv in (None, "") else str(int(iv))
            data["cam_%d" % i] = cam
            data["cmd_%d" % i] = cmd
            data["interval_%d" % i] = interval
            data["format_%d" % i] = fmt
        try:
            self._post_form("/cgi-bin/macro_save", data)
        except PanelError as e:
            # Un 400 sur macro_save (Referer toujours présent) = séquence refusée par le
            # firmware, pas un problème de transport. On le dit clairement.
            if "400" in str(e):
                raise PanelError("le pupitre a refusé cette macro : séquence d'étapes "
                                 "invalide pour ce firmware") from e
            raise
        return True

    def macro_control(self, index, play):
        self._post_form("/cgi-bin/macro_control",
                        {"select": str(int(index)), "operate": "1" if play else "0"})
        return True
