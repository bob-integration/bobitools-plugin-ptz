# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Pilote Sony — CGI HTTP (« command »/« inquiry »), génération « web SDK » / CGI v8.

Les tourelles Sony récentes (ILME-FR7, série SRG-A/X, BRC-X…) n'ont PAS besoin de VISCA
pour être pilotées : elles exposent une API CGI HTTP complète, servie par le même socle web
(`stm6`/`liveviewer`, `CGIVersion 8.x`) :

    GET /command/inquiry.cgi?inq=<groupe>   → texte « clé=valeur&clé=valeur »
    GET /command/<cgi>?<Param>=<valeur>     → 204 si la commande est acceptée

C'est la même philosophie que le pilote Panasonic (`panasonic_aw`), transport différent :
tout passe en HTTP, authentifié en Basic ou Digest. Le pilote `sony_visca` reste à côté pour
les caméras réellement pilotées en VISCA over IP (UDP 52381) ; sur une FR7 dont le VISCA
n'est pas activé, ce pilote-ci prend le relais sans rien exiger de plus que le web.

## Ce qui est vérifié et ce qui ne l'est pas

Confronté à une **ILME-FR7 réelle** (`10.10.11.7`, soft 4.00, `CGIVersion 8.0.1.0.0`) le
2026-08-04. Vérifié EN DIRECT (réponses relevées) :

- identité (`inq=system` : ModelName, Serial, SoftVersion) et réseau (`inq=network`) ;
- liste + **noms** des 100 mémoires (`inq=presetposition` : `PresetNum`, `PresetName`) ;
- **rappel de mémoire** — `presetposition.cgi?PresetCall=<n>` : la caméra bouge bien
  (positions `AbsolutePTZF` distinctes relevées avant/après sur les mémoires 1 et 2) ;
- **création/enregistrement** — `presetposition.cgi?PresetSet=<n>,<nom>,on` renvoie 204
  (le simple `PresetSet=<n>` répond **400** : c'était le bug de « création » remonté) ;
- **suppression** — `presetposition.cgi?PresetClear=<n>` renvoie 204 ;
- **renommage** — `presetposition.cgi?PresetName=<n>,<nom>` renvoie 204 ;
- **marche/veille** — `main.cgi?System=on|standby` renvoie 204 (testé `System=on`, déjà
  allumée). ATTENTION : `system.cgi?Power=on|off`, essayé au début, renvoie 204 mais N'AGIT
  PAS (no-op accepté) — c'est pour ça que le bouton de veille « ne marchait pas » ;
- écriture CGI en général — `tally.cgi?GTallyControl=turn_off` renvoie 204.

Toutes les commandes ci-dessus sont désormais VÉRIFIÉES en direct sur la FR7.

Réglages « Rec Format » (fréquence, mode de capteur FF/S35, codec, résolution) : lus et
ÉCRITS via `project.cgi?<Champ>=<valeur>` puis `project.cgi?SendProjectAndReload=execute`
(la caméra se recharge). Ils sont fortement INTERDÉPENDANTS ; on ne recopie pas le graphe de
contraintes (piège « plausible mais faux » déjà vu chez Panasonic) — on propose les valeurs
candidates et la caméra refuse d'elle-même les combinaisons invalides. La liste des
résolutions est fournie dynamiquement par la caméra. Mécanique vérifiée en direct (aller-
retour 1920x1080p ↔ 4096x2160p). Sorties SDI/HDMI (`MonitoringOutputFormat`) : idem, éditables.

Volontairement pas encore couvert : le CHANGEMENT d'adresse IP (le CGI réseau en écriture
n'a pas été validé sans risque — une IP mal écrite coupe la caméra). La LECTURE est là.
"""
import re
import time

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .base import (
    CAP_CONSOLE, CAP_PARAMS, CAP_POWER, CAP_PRESET_NAMES, CAP_PRESETS, CAP_PTZ,
    DriverError, PtzDriver, Unsupported, param, register,
)

# Modèles connus partageant le CGI v8. Seule la FR7 est VALIDÉE ; les autres sont déclarés
# parce qu'ils exposent la même API (même socle web SDK Sony), à confirmer le jour venu.
MODELS = [
    ("", "Auto (détecté à l'identification)"),
    ("ILME-FR7", "Sony ILME-FR7 (PTZ plein format, monture E)"),
    ("SRG-A40", "Sony SRG-A40 (4K, auto-framing)"),
    ("SRG-A12", "Sony SRG-A12 (4K, auto-framing)"),
    ("SRG-X400", "Sony SRG-X400 (4K)"),
    ("SRG-X120", "Sony SRG-X120 (4K)"),
    ("SRG-401SE", "Sony SRG-401SE (4K)"),
    ("BRC-X400", "Sony BRC-X400 (4K)"),
    ("BRC-X1000", "Sony BRC-X1000 (4K)"),
]


def _reason(exc):
    """Message court et lisible pour une panne réseau `requests`."""
    r = getattr(exc, "args", [exc])
    return str(r[0]) if r else str(exc)


# Suffixes de balayage d'une sortie Sony (après la résolution). « plva »/« plvb » = 3G-SDI
# niveau A/B — à garder DISTINCTS (sinon deux options d'enum porteraient le même libellé).
_SCAN_LABEL = {"p": "p", "i": "i", "ipsf": " PsF", "psf": " PsF", "plva": "p LVA", "plvb": "p LVB"}


def _mon_field(part):
    """« s1920ipsf » → « SDI 1920 PsF », « h1920i » → « HDMI 1920i », « snone » → « SDI off »."""
    kind = {"s": "SDI", "h": "HDMI"}.get(part[:1], "")
    rest = part[1:]
    if rest == "none":
        return f"{kind} off"
    m = re.match(r"(\d+)(\w*)", rest)
    if not m:
        return f"{kind} {rest}"
    res, scan = m.group(1), m.group(2)
    scan = _SCAN_LABEL.get(scan, scan)
    return f"{kind} {res}{scan}"


def _mon_label(value):
    """« s1920ipsf_h1920i » → « SDI 1920 PsF / HDMI 1920i » (SDI et HDMI couplés)."""
    return " / ".join(_mon_field(p) for p in value.split("_"))


# Réglages « Rec Format » (inq=project). Ces réglages sont INTERDÉPENDANTS et filtrés par la
# caméra elle-même (le RAW n'existe qu'en plein format, le S35 disparaît à 24 Hz, la liste des
# résolutions dépend de la fréquence/du scan…). Plutôt que de recopier ce graphe de contraintes
# — le piège « plausible mais faux » déjà rencontré côté Panasonic — on propose les valeurs
# candidates et on LAISSE LA CAMÉRA REFUSER les combinaisons invalides (erreur remontée telle
# quelle). La liste des résolutions, elle, est fournie DYNAMIQUEMENT par la caméra
# (`RecFormatVideoFormatList`) et reflète donc déjà l'état courant.
_FREQ_OPTS = [("5994", "59.94 Hz"), ("5000", "50 Hz"), ("2997", "29.97 Hz"),
              ("2500", "25 Hz"), ("2400", "24 Hz"), ("2398", "23.98 Hz")]
_CODEC_OPTS = [("xavc-i", "XAVC-I"), ("xavc-l", "XAVC-L"),
               ("raw", "RAW"), ("raw_xavc-i", "RAW & XAVC-I")]
_SCAN_OPTS = [("ff", "Plein format (FF)"), ("s35", "Super 35 (S35)")]

# Modes de prise de vue qui AUTORISENT le RAW (avec capteur plein format). Extrait du JS.
_RAW_MODES = {"cine_ei", "flexible_iso", "cine_ei_quick"}

# Clé de paramètre du plugin → nom du champ « Rec Format » chez Sony (écriture project.cgi).
_REC_PARAM = {
    "rec_frequency": "RecFormatFrequency",
    "rec_imager_scan": "RecFormatImagerScanMode",
    "rec_codec": "RecFormatCodec",
    "rec_video_format": "RecFormatVideoFormat",
}

# --------------------------------------------------------------------------- couleur (paint)
# Réglages colorimétriques : clé plugin → (cgi d'écriture, champ Sony, groupe d'inquiry de
# lecture). Vérifié sur ILME-FR7 : WB dans inq/imaging.cgi, noir dans inq/paint.cgi, Base Look
# (la « courbe/matrice » du cinéma = une LUT) dans inq/baselook.cgi. Le switch de base ISO
# (demandé pour bascule rapide haute/basse) = ExposureBaseSensitivity, dans imaging.
_COLOR = {
    "wb_mode":       ("imaging.cgi",  "WhiteBalanceMode",       "imaging"),
    "wb_color_temp": ("imaging.cgi",  "WhiteBalanceColorTemp",  "imaging"),
    "wb_tint":       ("imaging.cgi",  "WhiteBalanceTint",       "imaging"),
    "wb_cb_gain":    ("imaging.cgi",  "WhiteBalanceCbGain",     "imaging"),
    "wb_cr_gain":    ("imaging.cgi",  "WhiteBalanceCrGain",     "imaging"),
    "black_master":  ("paint.cgi",    "MasterBlack",            "paint"),
    "black_r":       ("paint.cgi",    "RBlack",                 "paint"),
    "black_b":       ("paint.cgi",    "BBlack",                 "paint"),
    "base_look":     ("baselook.cgi", "BaseLookCurrentBaseLook", "baselook"),
    "base_iso":      ("imaging.cgi",  "ExposureBaseSensitivity", "imaging"),
    "gain":          ("imaging.cgi",  "ExposureGain",           "imaging"),
    # Exposition — vérifié EN DIRECT sur la FR7 (2026-08-04). Seul l'iris (valeur BRUTE
    # ExposureIris, PAS ExposureFNumber qui est en lecture seule) et l'obturateur (mode +
    # angle) sont réellement inscriptibles ; le filtre ND est piloté par le sélecteur
    # physique (écriture CGI acceptée mais sans effet, Pmt=disable) → exposé en lecture seule.
    "iris":          ("imaging.cgi",  "ExposureIris",           "imaging"),
    "shutter_mode":  ("imaging.cgi",  "ExposureShutterMode",    "imaging"),
    "shutter_angle": ("imaging.cgi",  "ExposureAngle",          "imaging"),
    "nd_mode":       ("imaging.cgi",  "ExposureNDFilterMode",   "imaging"),
    "nd_variable":   ("imaging.cgi",  "ExposureNDVariable",     "imaging"),
}
_WB_MODE_OPTS = [("memory_a", "Mémoire A"), ("memory_b", "Mémoire B"),
                 ("preset", "Preset"), ("auto", "Auto (ATW)")]
_BASE_ISO_OPTS = [("low", "Base ISO basse"), ("high", "Base ISO haute")]
# Modes d'obturateur (imaging.cgi?ExposureShutterMode). « speed »/« angle » vérifiés en direct ;
# « ecs » proposé mais refusé (400) quand la fréquence ne l'autorise pas — la caméra tranche.
_SHUTTER_MODE_OPTS = [("speed", "Vitesse"), ("angle", "Angle"), ("ecs", "ECS")]
# Mode du filtre ND (lecture seule — cf. commentaire du dict _COLOR).
_ND_MODE_OPTS = [("clear", "Clair"), ("variable", "Variable"), ("preset", "Préréglage")]


def _baselook_opts(raw):
    """`PresetBaseLookList` (« id,base64label,enabled,… ») → [{value,label}]. Le libellé est
    encodé en base64 (ex. « Uy1DaW5ldG9uZQ== » = « S-Cinetone »)."""
    import base64
    toks = (raw or "").split(",")
    out = []
    for i in range(0, len(toks) - 2, 3):
        vid, b64, enabled = toks[i], toks[i + 1], toks[i + 2]
        if enabled != "1" or not vid:
            continue
        try:
            label = base64.b64decode(b64).decode("utf-8", "replace")
        except Exception:      # noqa: BLE001
            label = vid
        out.append({"value": vid, "label": label or vid})
    return out


def _parse_kv(text):
    """« a=1&b=2&c= » → {'a':'1','b':'2','c':''}. Format des réponses `inquiry.cgi`."""
    out = {}
    for pair in (text or "").split("&"):
        if "=" in pair:
            k, _, v = pair.partition("=")
            out[k.strip()] = v.strip()
    return out


@register
class SonyCgi(PtzDriver):
    KIND = "sony_cgi"
    LABEL = "Sony (CGI HTTP)"
    BRAND = "Sony"
    AVAILABLE = True
    DEFAULT_PORT = 80
    NEEDS_AUTH = True
    PRESET_RANGE = (1, 100)
    NOTE = ("Pilotage HTTP/CGI (pas de VISCA requis). Validé sur ILME-FR7 ; les modèles "
            "SRG/BRC partagent la même API mais restent à confirmer en exploitation.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._session = requests.Session()
        self._auth_mode = None       # None = indéterminé, "" = sans auth, "basic"/"digest"
        self._identity = None

    @classmethod
    def models(cls):
        return [{"value": v, "label": l, "family": ""} for v, l in MODELS]

    # -- transport -----------------------------------------------------------

    def _auths(self):
        """Stratégies d'auth à essayer, dans l'ordre, puis mémorisées.

        Digest EN PREMIER : vérifié sur ILME-FR7, la caméra n'accepte le Basic que pour les
        inquiries « live » (system, camera) et exige le Digest pour les endpoints privilégiés
        (network, presetposition, commandes). Le Digest, lui, passe PARTOUT — commencer par
        Basic ferait figer « basic » sur la première requête et refuser tout le reste."""
        if self._auth_mode == "":
            return [None]
        if self._auth_mode == "basic":
            return [HTTPBasicAuth(self.user, self.password)]
        if self._auth_mode == "digest":
            return [HTTPDigestAuth(self.user, self.password)]
        if self.user:
            return [HTTPDigestAuth(self.user, self.password),
                    HTTPBasicAuth(self.user, self.password), None]
        return [None]

    def _remember(self, auth):
        self._auth_mode = ("" if auth is None
                           else "basic" if isinstance(auth, HTTPBasicAuth) else "digest")

    def _request(self, path):
        """GET `/command/<path>` (ou chemin absolu). Renvoie (status, texte).

        Les inquiry.cgi répondent 200 + corps ; les commandes de contrôle répondent 204
        (corps vide). Un 401 fait basculer sur la stratégie d'auth suivante."""
        if not path.startswith("/"):
            path = "/command/" + path
        url = f"http://{self.host}:{self.port}{path}"
        # Referer vers la racine : inoffensif, et certains socles Sony le vérifient.
        headers = {"Referer": f"http://{self.host}/", "User-Agent": "bobitools-ptz"}
        last = None
        for auth in self._auths():
            try:
                r = self._session.get(url, auth=auth, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                raise DriverError(f"{self.host} injoignable : {_reason(e)}") from e
            if r.status_code == 401:
                last = "authentification refusée (identifiants ou mode d'accès)"
                continue
            if r.status_code >= 400:
                raise DriverError(f"HTTP {r.status_code} sur {path}")
            self._remember(auth)
            return r.status_code, r.text.strip()
        raise DriverError(last or "authentification impossible")

    def _inq(self, group):
        """Groupe d'inquiry → dict. `inquiry.cgi?inq=<group>` renvoie « k=v&k=v »."""
        _st, txt = self._request(f"inquiry.cgi?inq={group}")
        return _parse_kv(txt)

    def _cmd(self, cgi, **params):
        """Commande de contrôle. Succès attendu = 204 ; un 200 renvoyant un corps d'erreur
        (« inq… », « Error… ») est traité comme un refus explicite."""
        from urllib.parse import quote
        query = "&".join(f"{k}={quote(str(v), safe=',')}" for k, v in params.items())
        st, txt = self._request(f"{cgi}?{query}")
        if st != 204 and txt and ("error" in txt.lower() or txt.startswith("inq")):
            raise DriverError(f"commande refusée : {txt[:120]}")
        return txt

    # -- identité & disponibilité --------------------------------------------

    def reachable(self):
        """Sondage léger : une inquiry système suffit à prouver hôte + auth + caméra Sony."""
        self.last_error = None
        try:
            return bool(self._inq("system").get("ModelName"))
        except DriverError as e:
            self.last_error = str(e)
            return False

    def identify(self):
        sysinfo = self._inq("system")
        try:
            net = self._inq("network")
        except DriverError:
            net = {}
        model = sysinfo.get("ModelName") or ""
        soft = sysinfo.get("SoftVersion") or ""
        build = sysinfo.get("BuildNumber") or ""
        firmware = soft if not build else (f"{soft} ({build})" if soft else build)
        ident = {
            "model": model,
            "firmware": firmware,
            "serial": sysinfo.get("Serial") or "",
            "mac": net.get("MacAddress") or "",
            "name": net.get("CameraName") or "",
        }
        if not any(ident.values()):
            raise DriverError("aucune réponse d'identification "
                              "(caméra éteinte, adresse erronée ou accès refusé)")
        self._identity = ident
        if model and not self.model:
            self.model = model
        return ident

    def capabilities(self):
        caps = {CAP_POWER, CAP_PTZ, CAP_PRESETS, CAP_PARAMS, CAP_CONSOLE}
        # Sony stocke les noms de mémoires DANS la caméra (inq=presetposition → PresetName),
        # lus et réécrits sur la FR7. Déclaré sans I/O (vrai pour toute la génération CGI v8).
        caps.add(CAP_PRESET_NAMES)
        return caps

    # -- mémoires ------------------------------------------------------------

    def _preset_names(self):
        """{index(int): nom} lu depuis `inq=presetposition` (`PresetName=1,Preset1,2,…`)."""
        info = self._inq("presetposition")
        tokens = [t for t in (info.get("PresetName") or "").split(",") if t != ""]
        names = {}
        for i in range(0, len(tokens) - 1, 2):
            try:
                names[int(tokens[i])] = tokens[i + 1]
            except (ValueError, IndexError):
                continue
        try:
            count = int(info.get("PresetNum") or 0)
        except ValueError:
            count = 0
        return names, count

    def presets(self):
        lo, hi = self.PRESET_RANGE
        try:
            names, count = self._preset_names()
        except DriverError:
            names, count = {}, 0
        if count:
            hi = min(hi, count) if hi else count
        # Une mémoire nommée dans la caméra est une mémoire occupée ; les autres sont libres
        # (la FR7 ne liste que les mémoires définies dans PresetName).
        return [{"index": i, "name": names.get(i, ""), "used": i in names}
                for i in range(lo, hi + 1)]

    def _check(self, index):
        lo, hi = self.PRESET_RANGE
        i = int(index)
        if not lo <= i <= hi:
            raise DriverError(f"mémoire hors bornes : {i} (attendu {lo}..{hi})")
        return i

    def recall_preset(self, index):
        return self._cmd("presetposition.cgi", PresetCall=self._check(index))

    def store_preset(self, index, name=None):
        """Crée / réenregistre une mémoire à la position courante.

        Format vérifié en direct : « PresetSet=<id>,<nom>,on » (le simple « PresetSet=<id> »
        répond 400 — c'était le bug remonté). La caméra EXIGE un nom : à défaut on reprend
        celui déjà en place, sinon le défaut Sony « Preset<n> », pour ne pas écraser un nom
        existant lors d'un simple ré-enregistrement de position."""
        i = self._check(index)
        label = name
        if not label:
            try:
                label = self._preset_names()[0].get(i)
            except DriverError:
                label = None
        label = (label or f"Preset{i}").replace(",", " ").strip()
        return self._cmd("presetposition.cgi", PresetSet=f"{i},{label},on")

    def rename_preset(self, index, name):
        i = self._check(index)
        # Format vérifié en direct : « PresetName=<n>,<nom> ». Une virgule dans le nom
        # casserait la liste renvoyée par la caméra → on l'interdit ici.
        clean = (name or "").replace(",", " ").strip()
        return self._cmd("presetposition.cgi", PresetName=f"{i},{clean}")

    def clear_preset(self, index):
        """Supprime une mémoire (et sa vignette). Vérifié : « PresetClear=<n> » → 204."""
        i = self._check(index)
        return self._cmd("presetposition.cgi", PresetClear=i, PresetThumbnailClear=i)

    # -- marche / veille -----------------------------------------------------

    def power(self, on):
        """Marche / mise en veille : « main.cgi?System=on » / « main.cgi?System=standby »
        (commande trouvée dans le JS de la caméra puis vérifiée en direct — le
        « system.cgi?Power=on/off » essayé au début renvoie 204 mais N'AGIT PAS).

        Deux réalités mesurées sur la FR7 au RÉVEIL :
        1. envoyée pendant la transition (juste après une mise en veille), la commande peut
           répondre 400 le temps que la bascule s'achève → on réessaie brièvement ;
        2. le réveil est LENT : l'imageur met ~30–60 s à repartir. La commande renvoie 204
           tout de suite mais `Power` reste « standby » un moment ; on ne bloque pas là-dessus,
           l'état se met à jour au sondage de fond suivant."""
        if not on:
            return self._cmd("main.cgi", System="standby")
        last = None
        for _ in range(3):
            try:
                return self._cmd("main.cgi", System="on")
            except DriverError as e:
                last = e
                time.sleep(2)
        raise last

    # -- réseau (lecture seule pour l'instant) -------------------------------

    def read_network(self):
        """Config réseau, normalisée sur les clés du pilote Panasonic (ip4_*) pour que l'UI
        l'affiche pareil. Le CGI Sony en ÉCRITURE n'est pas encore validé → pas de set_network
        (donc pas de capacité CAP_NETWORK, le bouton « Changer l'IP » reste masqué)."""
        net = self._inq("network")
        if not net.get("Ip"):
            raise DriverError("configuration réseau illisible")
        return {
            "ip4_addr": net.get("Ip", ""),
            "ip4_netmask": net.get("Subnetmask", ""),
            "ip4_gateway": net.get("Gateway", ""),
            "ip4_dhcp": "1" if (net.get("Dhcp") == "on") else "0",
            "mac": net.get("MacAddress", ""),
            "port": net.get("HttpPort", ""),
            "hostname": net.get("HostName", ""),
        }

    # -- paramètres ----------------------------------------------------------

    def params_schema(self):
        # L'alimentation n'est PAS un paramètre ici : elle a ses propres boutons Marche/Veille
        # (capacité CAP_POWER). En faire aussi un interrupteur créait deux contrôles concurrents.
        #
        # État courant lu UNE fois : il sert à MASQUER les options invalides. Le groupe
        # « Enregistrement » (Rec Format) est placé AVANT « Sortie » car la liste des sorties
        # SDI/HDMI dépend du Rec Format (demandé par le testeur).
        try:
            proj = self._inq("project")
        except DriverError:
            proj = {}
        freq = proj.get("RecFormatFrequency")
        scan = proj.get("RecFormatImagerScanMode")
        mode = proj.get("BaseSettingShootingMode")

        # Résolutions : liste DYNAMIQUE de la caméra (déjà filtrée selon fréquence/scan).
        res_opts = [{"value": v.strip(), "label": v.strip()}
                    for v in (proj.get("RecFormatVideoFormatList") or "").split(",") if v.strip()]

        # Masquage des options — logique EXTRAITE du JS de la caméra (pas devinée) :
        #  - à 24 Hz (fréquence 2400) : S35 et XAVC-L indisponibles ;
        #  - RAW / RAW+XAVC-I : seulement en Cine EI (mode ∈ _RAW_MODES) ET capteur plein format.
        scan_opts = [{"value": v, "label": l} for v, l in _SCAN_OPTS
                     if not (v == "s35" and freq == "2400")]
        raw_ok = (mode in _RAW_MODES) and (scan == "ff")
        codec_opts = []
        for v, l in _CODEC_OPTS:
            if v == "xavc-l" and freq == "2400":
                continue
            if v in ("raw", "raw_xavc-i") and not raw_ok:
                continue
            codec_opts.append({"value": v, "label": l})

        # Sorties SDI/HDMI : liste dynamique (dépend du Rec Format courant).
        out_opts = []
        try:
            for v in (self._inq("monitoring").get("MonitoringOutputFormatList") or "").split(","):
                v = v.strip()
                if v:
                    out_opts.append({"value": v, "label": _mon_label(v)})
        except DriverError:
            pass

        # Base Look : liste (LUT) fournie par la caméra, libellés en base64.
        try:
            baselook_opts = _baselook_opts(self._inq("baselook").get("PresetBaseLookList"))
        except DriverError:
            baselook_opts = []

        rec_help = ("Réglage « Rec Format ». Appliqué en direct (project.cgi + "
                    "SendProjectAndReload) : la CAMÉRA SE RECHARGE (~30 s) et refuse les "
                    "combinaisons invalides. Fréquence, scan, codec et résolution sont liés.")
        return [
            # --- Enregistrement (pilote tout le reste → EN PREMIER). Options filtrées selon
            #     l'état courant ; ordre de restauration : fréquence/scan AVANT la résolution.
            param("rec_frequency", "Fréquence", "enum", group="Enregistrement",
                  options=[{"value": v, "label": l} for v, l in _FREQ_OPTS],
                  writable=True, bulk=True, validated=True, heavy=True, order=10, help=rec_help),
            param("rec_imager_scan", "Mode de capteur", "enum", group="Enregistrement",
                  options=scan_opts, writable=bool(scan_opts), bulk=True,
                  validated=True, heavy=True, order=12, help=rec_help),
            param("rec_codec", "Codec", "enum", group="Enregistrement",
                  options=codec_opts, writable=bool(codec_opts), bulk=True,
                  validated=True, heavy=True, order=14, help=rec_help),
            param("rec_video_format", "Résolution d'enregistrement", "enum", group="Enregistrement",
                  options=res_opts, writable=bool(res_opts), bulk=bool(res_opts),
                  validated=True, heavy=True, order=16, help=rec_help),

            # --- Sortie (dépend du Rec Format). SDI et HDMI couplés (valeur combinée `s…_h…`).
            param("sdi_hdmi_output", "Sortie SDI / HDMI", "enum", group="Sortie",
                  options=out_opts, writable=bool(out_opts), bulk=bool(out_opts),
                  validated=True, heavy=True, order=20,
                  help="monitoring.cgi?MonitoringOutputFormat. SDI et HDMI sont couplés : "
                       "la caméra n'accepte que ces combinaisons (liste selon le Rec Format)."),

            param("lens", "Objectif", "text", group="Optique", writable=False,
                  validated=True, help="Lecture inq=system (LensModelName)."),

            # --- Colorimétrie (paint). Réunis dans l'onglet RCP ; aussi éditables ici. ---
            param("base_iso", "Base ISO", "enum", group="Exposition", color=True,
                  options=[{"value": v, "label": l} for v, l in _BASE_ISO_OPTS],
                  writable=True, bulk=True, validated=True, order=30,
                  help="Bascule base ISO basse/haute (imaging.cgi?ExposureBaseSensitivity)."),
            param("gain", "Gain", "int", group="Exposition", color=True,
                  min=-3, max=30, step=1, big=3, writable=True, bulk=True, validated=True, order=31,
                  help="imaging.cgi?ExposureGain. Bornes UI approximatives."),
            # --- Iris : valeur BRUTE ExposureIris (ExposureFNumber est en lecture seule).
            #     Vérifié en direct : 31743≈F4.0 (ouvert) … 30464≈F22 (fermé), 31487≈F5.6.
            param("iris", "Iris (diaphragme)", "int", group="Exposition", color=True,
                  min=30464, max=31743, step=1, big=100, writable=True, bulk=True, validated=True,
                  order=32,
                  help="imaging.cgi?ExposureIris (valeur brute ; exige l'iris manuel, "
                       "ExposureAutoIris=off). Plus la valeur est HAUTE, plus l'iris est OUVERT : "
                       "31743≈F4.0 (ouvert) … 30464≈F22 (fermé), 31487≈F5.6. La caméra aligne sur "
                       "le cran de diaphragme le plus proche. ExposureFNumber n'est pas "
                       "inscriptible (lecture seule). Vérifié en direct sur ILME-FR7."),
            # --- Obturateur : mode inscriptible (speed↔angle vérifié) ; l'angle n'est
            #     inscriptible qu'en mode « angle » (sinon écriture ignorée par la caméra).
            param("shutter_mode", "Obturateur — mode", "enum", group="Exposition", color=True,
                  options=[{"value": v, "label": l} for v, l in _SHUTTER_MODE_OPTS],
                  writable=True, bulk=True, validated=True, order=33,
                  help="imaging.cgi?ExposureShutterMode. « Vitesse » = obturateur au repos "
                       "(état off) ; « Angle » engage l'obturateur ; « ECS » selon la fréquence "
                       "(refusé si indisponible). Bascule Vitesse↔Angle vérifiée en direct."),
            param("shutter_angle", "Obturateur — angle", "int", group="Exposition", color=True,
                  min=1, max=29, step=1, big=5, writable=True, bulk=True, validated=True, order=34,
                  help="imaging.cgi?ExposureAngle (valeur/index brut de la caméra, bornes "
                       "ExposureAngleRange). Inscriptible UNIQUEMENT en mode obturateur « Angle » "
                       "(en mode « Vitesse » l'écriture est ignorée). Vérifié en direct."),
            # --- Filtre ND : NON inscriptible via CGI sur cette FR7 (piloté par le sélecteur ND
            #     physique — l'écriture répond 204 mais reste sans effet, Pmt=disable).
            #     Exposé en LECTURE SEULE pour que l'état ND reste visible.
            param("nd_mode", "Filtre ND — mode", "enum", group="Exposition", color=True,
                  options=[{"value": v, "label": l} for v, l in _ND_MODE_OPTS],
                  writable=False, validated=False, order=35,
                  help="imaging.cgi?ExposureNDFilterMode (LECTURE SEULE). Sur cette FR7, mode et "
                       "densité ND sont pilotés par le SÉLECTEUR ND PHYSIQUE : l'écriture CGI est "
                       "acceptée (204) mais SANS EFFET (Pmt=disable). Constaté en direct."),
            param("nd_variable", "Filtre ND — densité variable", "int", group="Exposition",
                  color=True, writable=False, validated=False, order=36,
                  help="imaging.cgi?ExposureNDVariable (LECTURE SEULE). Densité du ND variable "
                       "(0 = clair). Non inscriptible via CGI dans cet état — sélecteur ND "
                       "physique. Constaté en direct."),
            param("wb_mode", "Mode balance des blancs", "enum", group="Balance des blancs",
                  color=True, options=[{"value": v, "label": l} for v, l in _WB_MODE_OPTS],
                  writable=True, bulk=True, validated=True, order=40,
                  help="imaging.cgi?WhiteBalanceMode."),
            param("wb_color_temp", "Température (K)", "int", group="Balance des blancs",
                  color=True, unit="K", min=2000, max=15000, step=100, big=1000,
                  writable=True, bulk=True, validated=True, order=41,
                  help="imaging.cgi?WhiteBalanceColorTemp. Bornes UI approximatives."),
            param("wb_tint", "Teinte", "int", group="Balance des blancs", color=True,
                  min=-99, max=99, step=1, big=10, writable=True, bulk=True, validated=True, order=42,
                  help="imaging.cgi?WhiteBalanceTint."),
            # Gains Cr/Cb regroupés sur une ligne (triplet « wbgain ») : Cr = rouge, Cb = bleu.
            param("wb_cr_gain", "Gain Cr (rouge)", "int", group="Balance des blancs", color=True,
                  min=-99, max=99, step=1, big=10, triplet="wbgain", channel="R",
                  writable=True, bulk=True, validated=True, order=43,
                  help="imaging.cgi?WhiteBalanceCrGain."),
            param("wb_cb_gain", "Gain Cb (bleu)", "int", group="Balance des blancs", color=True,
                  min=-99, max=99, step=1, big=10, triplet="wbgain", channel="B",
                  writable=True, bulk=True, validated=True, order=44,
                  help="imaging.cgi?WhiteBalanceCbGain."),
            param("black_master", "Noir maître", "int", group="Noir", color=True,
                  min=-99, max=99, step=1, big=10, writable=True, bulk=True, validated=True, order=50,
                  help="paint.cgi?MasterBlack."),
            # Noir R/B regroupés sur une ligne (triplet « black »).
            param("black_r", "Noir rouge", "int", group="Noir", color=True,
                  min=-99, max=99, step=1, big=10, triplet="black", channel="R",
                  writable=True, bulk=True, validated=True, order=51, help="paint.cgi?RBlack."),
            param("black_b", "Noir bleu", "int", group="Noir", color=True,
                  min=-99, max=99, step=1, big=10, triplet="black", channel="B",
                  writable=True, bulk=True, validated=True, order=52, help="paint.cgi?BBlack."),
            param("base_look", "Base Look", "enum", group="Look", color=True,
                  options=baselook_opts, writable=bool(baselook_opts), bulk=bool(baselook_opts),
                  validated=True, order=60,
                  help="baselook.cgi?BaseLookCurrentBaseLook (la LUT/look de la caméra)."),
        ]

    def read_params(self, keys=None):
        want = set(keys) if keys else None
        out = {}

        def wanted(k):
            return want is None or k in want

        # Clés issues de inq=project : les 4 Rec Format du schéma PLUS celles de la VUE
        # D'ENSEMBLE (`video_freq` = fréquence, `video_format` = format général = résolution
        # d'enregistrement). Une seule lecture si l'une d'elles est demandée.
        proj_map = dict(_REC_PARAM)
        proj_map["video_freq"] = "RecFormatFrequency"
        proj_map["video_format"] = "RecFormatVideoFormat"
        proj_keys = [k for k in proj_map if wanted(k)]
        if proj_keys:
            try:
                proj = self._inq("project")
            except DriverError:
                proj = {}
            for k in proj_keys:
                out[k] = proj.get(proj_map[k]) or None

        # Sortie SDI/HDMI : clé du schéma (`sdi_hdmi_output`, valeur brute pour l'enum) ET
        # clé de la vue d'ensemble (`video_format_sdi`, libellé lisible).
        if wanted("sdi_hdmi_output") or wanted("video_format_sdi"):
            try:
                mv = self._inq("monitoring").get("MonitoringOutputFormat")
            except DriverError:
                mv = None
            if wanted("sdi_hdmi_output"):
                out["sdi_hdmi_output"] = mv or None
            if wanted("video_format_sdi"):
                out["video_format_sdi"] = _mon_label(mv) if mv else None

        if wanted("lens"):
            try:
                out["lens"] = self._inq("system").get("LensModelName") or None
            except DriverError:
                out["lens"] = None

        # Réglages couleur : regroupés par groupe d'inquiry (imaging/paint/baselook) → une
        # lecture par groupe réellement demandé.
        color_keys = [k for k in _COLOR if wanted(k)]
        groups = {}
        for k in color_keys:
            groups.setdefault(_COLOR[k][2], []).append(k)
        for grp, ks in groups.items():
            try:
                data = self._inq(grp)
            except DriverError:
                data = {}
            for k in ks:
                v = data.get(_COLOR[k][1])
                if v is None or v == "":
                    out[k] = None
                else:
                    # Les valeurs numériques (température, gains, noir…) doivent être des ENTIERS
                    # pour les boutons/décalage du RCP ; les enums (mode, look…) restent en texte.
                    try:
                        out[k] = int(v)
                    except (TypeError, ValueError):
                        out[k] = v
        return out

    # -- vue d'ensemble ------------------------------------------------------
    # La vue d'ensemble transverse lit `video_freq` + un `video_format[_<sortie>]` par sortie
    # déclarée, et étiquette la fréquence via `freq_labels()`. On expose donc deux « sorties »
    # logiques — le format d'enregistrement (général) et la sortie SDI/HDMI — pour y faire
    # apparaître les trois infos demandées (fréquence, format général, format sortie).

    def outputs(self):
        return [
            {"value": "", "label": "Format général"},
            {"value": "sdi", "label": "Sortie SDI/HDMI"},
        ]

    def freq_labels(self):
        return {code: label for code, label in _FREQ_OPTS}

    def write_param(self, key, value):
        if key == "sdi_hdmi_output":
            return self._cmd("monitoring.cgi", MonitoringOutputFormat=str(value).strip())
        if key in _REC_PARAM:
            return self._set_rec(_REC_PARAM[key], str(value).strip())
        if key in _COLOR:
            cgi, field, _grp = _COLOR[key]
            return self._cmd(cgi, **{field: str(value).strip()})
        if key == "power":     # compat rappel groupé (l'UI passe par les boutons Marche/Veille)
            return self.power(value in (True, 1, "1", "on", "true", "True"))
        raise Unsupported(f"paramètre non inscriptible : {key}")

    def _set_rec(self, sony_param, value):
        """Écrit un « Rec Format » : on POSE la valeur puis on APPLIQUE avec
        SendProjectAndReload (la caméra se recharge). Vérifié en direct sur la FR7
        (RecFormatVideoFormat 1920x1080p ↔ 4096x2160p). La caméra refuse d'elle-même les
        combinaisons invalides — l'erreur remonte telle quelle."""
        self._cmd("project.cgi", **{sony_param: value})
        return self._cmd("project.cgi", SendProjectAndReload="execute")

    # -- console (mise au point / rétro-ingénierie) --------------------------

    def console(self, cmd, channel=""):
        """Envoie une requête CGI brute et renvoie la réponse.

        `cmd` : soit un chemin absolu (« /command/inquiry.cgi?inq=camera »), soit une forme
        courte (« inquiry.cgi?inq=camera », « presetposition.cgi?PresetCall=1 »). C'est
        l'outil qui a servi à valider cette table — il reste là pour l'étendre."""
        c = (cmd or "").strip()
        if not c:
            raise DriverError("commande vide")
        st, txt = self._request(c)
        return {"cmd": c, "channel": "cgi", "response": f"[{st}] {txt}"[:2000],
                "error": st >= 400}
