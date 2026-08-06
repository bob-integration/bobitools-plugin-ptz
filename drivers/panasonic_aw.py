# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Pilote Panasonic — protocole « AW » sur CGI HTTP.

Toutes les tourelles Panasonic (AW-HE… comme AW-UE…) exposent le même triptyque :

    /cgi-bin/aw_ptz?cmd=%23<CMD>&res=1     commandes tourelle (préfixe « # »)
    /cgi-bin/aw_cam?cmd=<CMD>&res=1        commandes caméra (requêtes préfixées « Q »)
    /cgi-bin/getinfo?FILE=1                identité (modèle, firmware, MAC)

La réponse est du texte brut, jamais du JSON : `#O` (marche/veille) répond `p1`, `#R05`
répond `s05`, une commande refusée répond `ER1:…`/`rER`. Le pilote traduit ça en valeurs
typées et en `DriverError` explicites.

## Ce qui est vérifié et ce qui ne l'est pas

Tout ce qui porte `validated=True` a été CONFRONTÉ À UNE AW-HE130 RÉELLE (firmware
V02.3800) le 2026-07-29 : marche/veille, autofocus, vitesse de rappel des mémoires, gain,
filtre ND, obturateur, position zoom, et le couple format/fréquence vidéo.

Deux méthodes ont servi, aucune ne repose sur la documentation publique :

1. **énumération exhaustive** de l'espace des requêtes `Q`+2 lettres (676 combinaisons,
   uniquement des lectures) → 36 requêtes supportées, cf. `PROBE` ;
2. **lecture des pages de réglages que la caméra sert elle-même**, qui contiennent le code
   JavaScript appelant les CGI — c'est là qu'on trouve `OSA:87` (format) et `OSE:77`
   (fréquence) avec leur table de codes, introuvables par la seule énumération puisque ces
   commandes prennent un paramètre.

Ce qui reste `validated=False` est signalé comme tel dans l'UI, et `console()` / `probe()`
sont là pour lever ces réserves de la même façon sur les autres modèles. Une commande
vérifiée sur HE130 ne l'est pas pour autant sur UE160 : les tables dépendantes de la
génération (formats vidéo) sont explicitement séparées par famille.
"""
import re
import threading
import time

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .base import (
    CAP_CONSOLE, CAP_NETWORK, CAP_PARAMS, CAP_POWER, CAP_PRESET_NAMES, CAP_PRESETS, CAP_PTZ, CAP_SNAPSHOT,
    DriverError, PtzDriver, Unsupported, param, register,
)
from .panasonic_outputs import CTX_FPS, CTX_UHD_CROP, OUT_FORMATS, SYS_FORMATS

# Familles de modèles. Le protocole de base est commun ; seuls les paramètres « caméra »
# et les formats vidéo disponibles diffèrent.
FAMILY_UE = "ue"          # génération Ultra/4K et récentes HD : AW-UE150/UE160/UE100/UE40…
FAMILY_HE = "he"          # génération HD antérieure : AW-HE130/HE40/HE42…

MODELS = [
    ("", "Auto (détecté à l'identification)", ""),
    ("AW-UE160", "AW-UE160 (4K, ST 2110 en option)", FAMILY_UE),
    ("AW-UE150", "AW-UE150 (4K, ST 2110 en option)", FAMILY_UE),
    ("AW-UE100", "AW-UE100 (4K, NDI|HX / SRT)", FAMILY_UE),
    ("AW-UE80", "AW-UE80 (4K, NDI|HX / SRT)", FAMILY_UE),
    ("AW-UE70", "AW-UE70 (4K)", FAMILY_UE),
    ("AW-UE50", "AW-UE50 (4K)", FAMILY_UE),
    ("AW-UE40", "AW-UE40 (4K)", FAMILY_UE),
    ("AW-UE20", "AW-UE20 (HD)", FAMILY_UE),
    ("AW-UE4", "AW-UE4 (HD, compacte)", FAMILY_UE),
    ("AW-HE130", "AW-HE130 (HD)", FAMILY_HE),
    ("AW-HE145", "AW-HE145 (HD)", FAMILY_HE),
    ("AW-HE140", "AW-HE140 (HD)", FAMILY_HE),
    ("AW-HE42", "AW-HE42 (HD)", FAMILY_HE),
    ("AW-HE40", "AW-HE40 (HD)", FAMILY_HE),
    ("AW-HE38", "AW-HE38 (HD)", FAMILY_HE),
]

# --------------------------------------------------------------------------- format vidéo
#
# Lecture  : QSA:87        → OSA:87:<hex>
# Écriture : OSA:87:<hex>
# Fréquence, lecture : QSE:77 → OSE:77:<0|1>   (0 = 59.94 Hz, 1 = 50 Hz)
# Fréquence, écriture : OSE:77:<0|1>
#
# La table code ↔ format n'est PAS devinée : elle est extraite de la page de réglages que
# la caméra sert elle-même (`/admin/setup_camera_system.html`, fonction `createFormatList`),
# puis vérifiée en direct — une AW-HE130 réglée en 1080/50i répond bien `OSA:87:05`, et la
# page injecte `sFormat = 0x05`. C'est donc la table de la caméra, pas une reconstitution.
#
# Les formats disponibles dépendent de la FRÉQUENCE : une caméra en 50 Hz ne propose que
# des formats 50 Hz. On n'expose donc que ceux de la fréquence courante, comme le fait
# l'interface de la caméra.
# Fréquences PAR FAMILLE. La HE130 n'en a que deux ; la UE160 en a cinq (relevé dans
# `getCurrentSystemFrequency` de sa page web, confirmé par QSE:77). Supposer partout le
# couple 59.94/50 rendrait « 24 Hz » illisible sur une UE — d'où une table par famille.
FREQS = {
    FAMILY_HE: {"0": "59.94 Hz", "1": "50 Hz"},
    FAMILY_UE: {"0": "59.94 Hz", "1": "50 Hz", "2": "24 Hz", "3": "23.98 Hz", "4": "60 Hz"},
}

# Table code → format PAR FAMILLE, extraite de la page web de chaque caméra puis vérifiée
# en direct. HE130 : `createFormatList` de `/admin/setup_camera_system.html`.
# UE160 : `getCurrentFormat` de `/js/pc/common_function.js` (37 codes).
# Les codes communs aux deux tables concordent (05=1080/50i, 11=1080/50p, 04=1080/59.94i…),
# ce qui les recoupe mutuellement.
FORMAT_CODES = {
    FAMILY_HE: {
        0x01: "720/59.94p", 0x02: "720/50p", 0x04: "1080/59.94i", 0x05: "1080/50i",
        0x07: "1080/29.97PsF", 0x08: "1080/25PsF", 0x0A: "1080/23.98PsF",
        0x0B: "480/59.94i", 0x0D: "576/50i", 0x10: "1080/59.94p", 0x11: "1080/50p",
        0x12: "480/59.94p(i)", 0x13: "576/50p(i)", 0x14: "1080/29.97p",
        0x15: "1080/25p", 0x16: "1080/23.98p",
    },
    FAMILY_UE: {
        0x00: "720/60p", 0x01: "720/59.94p", 0x02: "720/50p", 0x03: "1080/60i",
        0x04: "1080/59.94i", 0x05: "1080/50i", 0x06: "1080/30PsF", 0x07: "1080/29.97PsF",
        0x08: "1080/25PsF", 0x09: "1080/24PsF", 0x0A: "1080/23.98PsF",
        0x0B: "480/59.94i", 0x0C: "480/29.97PsF", 0x0D: "576/50i", 0x0E: "576/25PsF",
        0x10: "1080/59.94p", 0x11: "1080/50p", 0x12: "480/59.94p", 0x13: "576/50p",
        0x14: "1080/29.97p", 0x15: "1080/25p", 0x16: "1080/23.98p(over 59.94i)",
        0x17: "2160/29.97p", 0x18: "2160/25p", 0x19: "2160/59.94p", 0x1A: "2160/50p",
        0x1B: "2160/23.98p", 0x1C: "2160/29.97PsF", 0x1D: "2160/25PsF",
        0x1E: "2160/23.98PsF", 0x1F: "2160/60p", 0x20: "1080/60p", 0x21: "2160/24p",
        0x22: "1080/24p", 0x23: "1080/23.98p", 0x26: "1080/119.88p", 0x27: "1080/100p",
    },
}

# Formats SYSTÈME proposés à l'écriture, par famille et par fréquence. Les deux listes
# sont EXACTES, tirées du code de la caméra correspondante :
#   HE : `createFormatList` de `/admin/setup_camera_system.html` ;
#   UE : les `case` des fonctions `refresh*Format` (cf. panasonic_outputs.SYS_FORMATS).
#
# La distinction compte : sur UE160, `1080/50i` est un format de SORTIE valide mais PAS un
# format système — le lui envoyer répond `ER3`. Une liste déduite de la seule cadence
# (ce qu'on faisait avant) le proposait à tort.
FORMATS_HE = {
    "0": [0x10, 0x14, 0x16, 0x04, 0x07, 0x0A, 0x01, 0x12, 0x0B],
    "1": [0x11, 0x15, 0x05, 0x08, 0x02, 0x13, 0x0D],
}


def _formats(family, freq):
    """[(code, libellé)] proposables comme format SYSTÈME pour cette famille/fréquence."""
    codes = FORMAT_CODES.get(family, {})
    table = FORMATS_HE if family == FAMILY_HE else SYS_FORMATS
    return [(c, codes[c]) for c in table.get(freq, []) if c in codes]


def _format_label(family, freq, code):
    """Libellé d'un code. Cherché dans TOUTE la table de la famille, pas seulement dans les
    formats de la fréquence courante : une sortie peut porter un code d'une autre cadence."""
    return FORMAT_CODES.get(family, {}).get(code)


def _format_code(family, freq, label):
    for c, l in _formats(family, freq):
        if l == label:
            return c
    return None


# Sorties vidéo par famille : (clé, libellé, requête de lecture, préfixe de réponse).
# `""` = réglage général. Relevé dans `cparam.js` de la UE160 et confirmé en direct :
# chaque sortie a sa propre commande, et toutes partagent la table de codes ci-dessus
# (prouvé par `refresh12GSDISFPFormat`, qui compare aux mêmes codes "10"/"11"/"20").
#
# Une famille qui ne déclare que le réglage général produit un tableau d'une ligne — c'est
# le cas de la HE130, et c'est juste : elle n'a pas de format par sortie.
OUTPUTS = {
    FAMILY_HE: [
        ("", "Réglage général", "QSA:87", "OSA:87:"),
    ],
    FAMILY_UE: [
        ("", "Réglage général", "QSA:87", "OSA:87:"),
        ("12g", "12G SDI", "QSJ:1E", "OSJ:1E:"),
        ("3g1", "3G SDI OUT1", "QSJ:21", "OSJ:21:"),
        ("3g2", "3G SDI OUT2", "QSJ:23", "OSJ:23:"),
        ("hdmi", "HDMI", "QSJ:25", "OSJ:25:"),
        ("monitor", "Monitor", "QSL:AD", "OSL:AD:"),
        ("return", "Return", "QSL:B4", "OSL:B4:"),
    ],
}


# Commandes de LECTURE envoyées par `probe()`. Aucune n'est destructive : ce sont toutes
# des requêtes (convention AW : préfixe « Q » côté caméra, réponse « O… » côté tourelle).
#
# Cette table N'EST PAS devinée : elle vient d'une énumération exhaustive de l'espace
# Q+2 lettres (676 combinaisons) exécutée le 2026-07-29 sur une AW-HE130 réelle
# (firmware V02.3800). 36 requêtes y répondent, les voici. Le libellé dit ce qu'on SAIT ;
# « ? » marque une réponse valide dont la sémantique n'est pas établie — la connaître
# exactement demanderait de faire varier le réglage et d'observer la valeur.
PROBE = [
    ("ptz", "#O", "marche / veille"),
    ("ptz", "#APC", "position pan/tilt absolue"),
    ("ptz", "#GZ", "position zoom"),
    ("ptz", "#GF", "position focus"),
    ("ptz", "#D1", "autofocus"),
    ("ptz", "#D3", "? (répond d3x)"),
    ("ptz", "#DA", "? (répond dAx)"),
    ("ptz", "#UPVS", "vitesse de rappel des mémoires"),
    ("ptz", "#LPC", "? limites de course"),
    ("ptz", "#TAE", "? tally"),
    ("cam", "QID", "identifiant modèle"),
    ("cam", "QSV", "version firmware"),
    ("cam", "QAF", "autofocus"),
    ("cam", "QGU", "gain"),
    ("cam", "QFT", "filtre ND"),
    ("cam", "QSH", "obturateur"),
    ("cam", "QRS", "? mode iris"),
    ("cam", "QSA:87", "format vidéo"),
    ("cam", "QSE:77", "fréquence vidéo (0=59.94 Hz, 1=50 Hz)"),
    ("cam", "QSF", "?"),
    ("cam", "QAW", "? balance des blancs (mode)"),
    ("cam", "QAB", "?"), ("cam", "QAR", "?"), ("cam", "QBD", "?"),
    ("cam", "QBI", "?"), ("cam", "QBP", "?"), ("cam", "QBR", "?"),
    ("cam", "QCS", "?"), ("cam", "QDE", "?"), ("cam", "QDT", "?"),
    ("cam", "QER", "?"), ("cam", "QFB", "?"), ("cam", "QFS", "?"),
    ("cam", "QFZ", "?"), ("cam", "QGB", "?"), ("cam", "QGR", "?"),
    ("cam", "QHP", "?"), ("cam", "QIS", "?"), ("cam", "QMS", "?"),
    ("cam", "QRD", "?"), ("cam", "QRI", "?"), ("cam", "QRP", "?"),
    ("cam", "QRV", "?"), ("cam", "QSM", "?"), ("cam", "QTD", "?"),
    ("cam", "QTP", "?"), ("cam", "QUG", "?"), ("cam", "QUS", "?"),
]

_ERR = re.compile(r"^(ER\d|rER)", re.I)

# Longueur du champ « nom de mémoire » dans la caméra : la trame `OSJ:35:<NN>:<nom>`
# fait 18 caractères après le préfixe, dont 3 pour « NN: ». Mesuré sur AW-UE160.
PRESET_NAME_MAX = 15


@register
class PanasonicAW(PtzDriver):
    KIND = "panasonic_aw"
    LABEL = "Panasonic (protocole AW)"
    BRAND = "Panasonic"
    AVAILABLE = True
    DEFAULT_PORT = 80
    NEEDS_AUTH = True
    PRESET_RANGE = (1, 100)
    NOTE = ("Les commandes tourelle (mémoires, pan/tilt/zoom, marche/veille) sont communes "
            "à toute la gamme. Les paramètres caméra sont à confirmer par modèle : "
            "utilisez l'onglet Console.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._session = requests.Session()
        self._auth_mode = None       # None = pas encore déterminé, "" = sans auth
        self._identity = None
        self._freq = None            # fréquence vidéo, lue à la demande puis mémorisée
        self._ctx_cache = {}         # variables de contexte (FPS, recadrage UHD)

    # -- catalogue -----------------------------------------------------------

    @classmethod
    def models(cls):
        return [{"value": v, "label": l, "family": f} for v, l, f in MODELS]

    def family(self, model=None):
        m = (model or self.model or "").upper().replace(" ", "")
        for value, _label, fam in MODELS:
            if value and m.startswith(value.upper()):
                return fam
        if m.startswith("AW-HE") or m.startswith("AWHE"):
            return FAMILY_HE
        return FAMILY_UE          # défaut : la génération récente, la plus répandue

    # -- transport -----------------------------------------------------------

    def _auths(self):
        """Stratégies d'authentification à essayer, dans l'ordre. Les tourelles Panasonic
        sont configurables en accès libre, Basic ou Digest selon le modèle et le réglage
        « Contrôle d'accès » ; on découvre à la première requête puis on mémorise."""
        if self._auth_mode == "":
            return [None]
        if self._auth_mode == "basic":
            return [HTTPBasicAuth(self.user, self.password)]
        if self._auth_mode == "digest":
            return [HTTPDigestAuth(self.user, self.password)]
        candidates = [None]
        if self.user:
            candidates = [HTTPBasicAuth(self.user, self.password),
                          HTTPDigestAuth(self.user, self.password), None]
        return candidates

    def _remember(self, auth):
        self._auth_mode = ("" if auth is None
                           else "basic" if isinstance(auth, HTTPBasicAuth) else "digest")

    def _get(self, path, params=None, raw=False, session=None):
        url = f"http://{self.host}:{self.port}{path}"
        sess = session or self._session
        # Certaines pages (get_network…) exigent un `Referer` vers `/admin/` (anti-CSRF) ;
        # inoffensif pour les commandes aw_ptz/aw_cam, qui l'ignorent.
        headers = {"Referer": f"http://{self.host}/admin/index.html"}
        last = None
        for auth in self._auths():
            try:
                r = sess.get(url, params=params, auth=auth, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                raise DriverError(f"{self.host} injoignable : {_reason(e)}") from e
            if r.status_code == 401:
                last = "authentification refusée (identifiants ou mode d'accès)"
                continue
            if r.status_code >= 400:
                raise DriverError(f"HTTP {r.status_code} sur {path}")
            self._remember(auth)
            return r.content if raw else r.text.strip()
        raise DriverError(last or "authentification impossible")

    def _ptz(self, cmd):
        """Commande tourelle. Le « # » est transmis encodé (%23), comme attendu."""
        res = self._get("/cgi-bin/aw_ptz", {"cmd": cmd, "res": "1"})
        if _ERR.match(res):
            raise DriverError(f"commande refusée ({cmd}) : {res}")
        return res

    def _cam(self, cmd):
        res = self._get("/cgi-bin/aw_cam", {"cmd": cmd, "res": "1"})
        if _ERR.match(res):
            raise DriverError(f"commande refusée ({cmd}) : {res}")
        return res

    def _post(self, path, data):
        """POST form (config réseau…). Envoie un `Referer` vers `/admin/` (anti-CSRF possible,
        comme sur les pupitres), port par défaut OMIS pour un éventuel match strict."""
        url = f"http://{self.host}:{self.port}{path}"
        headers = {"Referer": f"http://{self.host}/admin/index.html"}
        last = None
        for auth in self._auths():
            try:
                r = self._session.post(url, data=data, auth=auth, headers=headers,
                                       timeout=self.timeout)
            except requests.RequestException as e:
                raise DriverError(f"{self.host} injoignable : {_reason(e)}") from e
            if r.status_code == 401:
                last = "authentification refusée (identifiants ou mode d'accès)"
                continue
            if r.status_code >= 400:
                raise DriverError(f"HTTP {r.status_code} sur {path}")
            self._remember(auth)
            return r.text.strip()
        raise DriverError(last or "authentification impossible")

    # -- réseau (lecture / changement d'adresse IP) --------------------------

    def read_network(self):
        """Config réseau lan0 : {ip4_addr, ip4_netmask, ip4_gateway, ip4_dhcp, port, …}."""
        kv = {}
        for line in self._get("/cgi-bin/get_network", {"interface": "lan0"}).splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.strip()
        if not kv.get("ip4_addr"):
            raise DriverError("configuration réseau illisible")
        return kv

    def set_network(self, ip=None, netmask=None, gateway=None, dhcp=None):
        """Change la config réseau (LECTURE-MODIFICATION-RÉÉCRITURE, pour préserver le reste).
        ATTENTION : la caméra bascule sur la nouvelle adresse — la connexion à l'ANCIENNE est
        perdue (et elle peut redémarrer). L'appelant doit ensuite la re-localiser."""
        kv = self.read_network()
        if dhcp is not None:
            kv["ip4_dhcp"] = "1" if dhcp else "0"
        if ip:
            kv["ip4_addr"] = ip
        if netmask:
            kv["ip4_netmask"] = netmask
        if gateway:
            kv["ip4_gateway"] = gateway
        self._post("/cgi-bin/network", kv)
        return kv

    # -- identité & disponibilité --------------------------------------------

    def reachable(self):
        """Sondage léger : `#O` (marche/veille) est la commande la moins coûteuse qui
        prouve à la fois que l'hôte répond ET que le CGI AW est bien celui d'une tourelle."""
        self.last_error = None
        try:
            return bool(self._ptz("#O"))
        except DriverError as e:
            self.last_error = str(e)
            return False

    def identify(self):
        """`getinfo` d'abord (le plus riche, souvent accessible sans authentification),
        puis repli sur `QID`/`QSV` si le modèle ne l'expose pas."""
        info = {}
        try:
            txt = self._get("/cgi-bin/getinfo", {"FILE": "1"})
            for line in txt.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    info[k.strip().upper()] = v.strip()
        except DriverError:
            pass
        # Vérifié sur AW-HE130 : `getinfo` ne renvoie PAS de clé MODEL. Le modèle est dans
        # `NAME` (« AW-HE130W » — variante de coloris comprise, donc plus précis que le
        # « AW-HE130 » de QID, gardé en repli). `NAME` n'est donc pas un nom d'exploitation
        # et n'a rien à faire dans le champ `name`.
        model = info.get("MODEL") or info.get("MODELNAME") or info.get("NAME") or ""
        firmware = info.get("VERSION") or info.get("FIRMWARE") or ""
        if not model:
            try:
                model = _after(self._cam("QID"), "OID:")
            except DriverError:
                pass
        if not firmware:
            try:
                firmware = _after(self._cam("QSV"), "OSV:")
            except DriverError:
                pass
        ident = {
            "model": model,
            "firmware": firmware,
            "serial": info.get("SERIAL") or info.get("SERIALNO") or "",
            "mac": info.get("MAC") or "",
            "name": info.get("CAMERANAME") or "",
        }
        if not any(ident.values()):
            # Tout est vide : la caméra n'a rien répondu du tout. Enregistrer une identité
            # vide la ferait passer pour « identifiée » dans l'UI — autant échouer clairement.
            raise DriverError("aucune réponse d'identification "
                              "(caméra éteinte, adresse erronée ou accès refusé)")
        self._identity = ident
        if model and not self.model:
            self.model = model
        return ident

    def capabilities(self):
        caps = {CAP_POWER, CAP_PTZ, CAP_PRESETS, CAP_PARAMS, CAP_SNAPSHOT, CAP_CONSOLE, CAP_NETWORK}
        # Nommage des mémoires DANS la caméra : vérifié présent sur AW-UE160
        # (`QSJ:35`/`OSJ:35`) et absent sur AW-HE130 (`ER1`). Déclaré par famille pour
        # rester sans I/O ; si un modèle UE plus modeste refuse la commande, `presets()` et
        # `rename_preset()` retombent proprement sur le nommage local.
        if self.family() == FAMILY_UE:
            caps.add(CAP_PRESET_NAMES)
        return caps

    # -- format vidéo --------------------------------------------------------

    def freq(self, refresh=False):
        """Fréquence vidéo courante ("0" = 59.94 Hz, "1" = 50 Hz), ou None si illisible.

        Mémorisée : la liste des formats en dépend, et elle est demandée à chaque
        construction du schéma. Elle ne change qu'au prix d'un redémarrage de la caméra."""
        if refresh or self._freq is None:
            try:
                self._freq = _after(self._cam("QSE:77"), "OSE:77:").strip()
            except DriverError:
                self._freq = None
        return self._freq

    def out_options(self, out_key):
        """Formats admissibles sur une sortie, ici et maintenant.

        La caméra n'accepte QUE ces valeurs : lui en envoyer une autre répond `ER2` — ce qui
        ressemble à « occupé » mais signifie « pas admissible dans cette configuration ».
        D'où la résolution des quatre variables de contexte plutôt qu'une liste figée."""
        table = OUT_FORMATS.get(out_key, {}).get(self.freq() or "", {})
        variants = table.get(self._sys_format_code() or "", {})
        if not variants:
            return []
        if variants.get("crop2") and self._ctx(CTX_UHD_CROP) == "2":
            return variants["crop2"]
        if variants.get("fps3") and self._ctx(CTX_FPS) == "3":
            return variants["fps3"]
        return variants.get("default", [])

    def _ctx(self, spec):
        """Valeur d'une variable de contexte (cadence, recadrage), mémorisée."""
        query, prefix = spec
        if query not in self._ctx_cache:
            try:
                self._ctx_cache[query] = (_after(self._cam(query), prefix) or "").strip()
            except DriverError:
                self._ctx_cache[query] = None
        return self._ctx_cache[query]

    def _sys_format_code(self):
        """Code hexadécimal du format système, en MAJUSCULES (clé des tables)."""
        try:
            raw = _after(self._cam("QSA:87"), "OSA:87:")
        except DriverError:
            return None
        return (raw or "").strip().upper() or None

    # -- paramètres ----------------------------------------------------------

    def outputs(self):
        return [{"value": k, "label": lbl}
                for k, lbl, _q, _p in OUTPUTS.get(self.family(), [])]

    def freq_labels(self):
        return FREQS.get(self.family(), FREQS[FAMILY_HE])

    # -- pedestals (paint), par génération ------------------------------------
    # HE : QTP/QRP/QBP en DÉCIMAL, centrés sur 096 (096 = neutre), pas de vert.
    # UE : QSJ:0F (maître) et QSG:4C/4D/4E (R/V/B) en HEXA centré sur 0x800 (0 = neutre),
    #      décodés en direct sur AW-UE160 (ex. maître 7EB = -21). Vérifié via cparam.js
    #      (cparam_get/set_*Pedestal) puis en lecture sur la caméra.
    def _peds(self):
        if self.family() == FAMILY_HE:
            return ({"ped_master": ("QTP", "OTP:"), "ped_r": ("QRP", "ORP:"),
                     "ped_b": ("QBP", "OBP:")}, False)
        return ({"ped_master": ("QSJ:0F", "OSJ:0F:"), "ped_r": ("QSG:4C", "OSG:4C:"),
                 "ped_g": ("QSG:4D", "OSG:4D:"), "ped_b": ("QSG:4E", "OSG:4E:")}, True)

    def _ped_read(self, key):
        peds, hx = self._peds()
        if key not in peds:
            return None
        raw = _after(self._cam(peds[key][0]), peds[key][1])
        if not raw:
            return None
        try:
            return int(raw, 16) - 0x800 if hx else int(raw)
        except ValueError:
            return None

    def _ped_write(self, key, value):
        peds, hx = self._peds()
        if key not in peds:
            raise Unsupported(f"pedestal non supporté sur ce modèle : {key}")
        ocmd = peds[key][1].rstrip(":")
        v = int(value)
        if hx:
            return self._cam("%s:%03X" % (ocmd, (v + 0x800) & 0xFFF))
        return self._cam("%s:%03d" % (ocmd, max(0, min(999, v))))

    # -- gain maître, par génération -----------------------------------------
    # HE : QGU/OGU (code brut). UE : QSL:25/OSL:25 en dB (code = dB + 8, vérifié AW-UE160 :
    # 0x12 → 10 dB, écriture 0-12 dB validée en direct). Encodage 2 hexa.
    def _gain(self):
        if self.family() == FAMILY_HE:
            return ("QGU", "OGU:", 0)
        return ("QSL:25", "OSL:25:", 8)

    def _gain_read(self):
        q, pfx, off = self._gain()
        raw = _after(self._cam(q), pfx)
        if not raw:
            return None
        try:
            return int(raw, 16) - off
        except ValueError:
            return None

    def _gain_write(self, value):
        q, pfx, off = self._gain()
        return self._cam("%s:%02X" % (pfx.rstrip(":"), (int(value) + off) & 0xFF))

    # -- knee (UE160) : ÉDITABLE, aller-retour validé en direct ---------------
    # point : QSA:20 → OSA:20:XX ; valeur = (int(XX,16) + 246) / 4 (ex. 0x72 = 90).
    # slope : QSA:24 → OSA:24:XX ; valeur = int(XX,16) (ex. 0x46 = 70).
    # Écriture sur 2 hexa majuscules, bornée à l'octet. Aller-retour 90→95→90 et
    # 70→80→70 confirmé sur l'AW-UE160 (10.10.11.6), valeurs restaurées.
    def _knee_read(self, which):
        query, prefix = ("QSA:20", "OSA:20:") if which == "point" else ("QSA:24", "OSA:24:")
        raw = _after(self._cam(query), prefix)
        if not raw:
            return None
        try:
            iv = int(raw, 16)
        except ValueError:
            return None
        return int(round((iv + 246) / 4.0)) if which == "point" else iv

    def _knee_write(self, which, value):
        v = int(value)
        if which == "point":
            code = max(0, min(0xFF, int(round(v * 4 - 246))))
            return self._cam("OSA:20:%02X" % code)
        return self._cam("OSA:24:%02X" % max(0, min(0xFF, v)))

    # -- painting (UE160) : ÉDITABLE, DÉPENDANT DU MODE, VÉRIFIÉ PAR RELECTURE ---
    # Encodages vérifiés en direct sur AW-UE160 (10.10.11.6) :
    #   température   : 5 hexa de tête, en Kelvin ;
    #   gains R/B     : centrés sur 0x800 (3 hexa) ;
    #   gamma noir    : centré sur 0x80 (2 hexa) ;
    #   niveau détail : centré sur 0x80 (2 hexa) ;
    #   chroma        : octet brut (2 hexa).
    #
    # Certaines commandes DÉPENDENT DU MODE de balance des blancs (QAW / OAW) :
    #   0=ATW, 2=AWC A, 3=AWC B, 4=3200K, 5=5600K, 9=VAR.
    #   - gain R/B : AWC A/B → OSJ:4B/4C ; VAR → OSG:39/3A (mêmes encodages) ;
    #                dans les autres modes, non pilotable ;
    #   - température : VAR → OSI:20 (éditable) ; AWC A/B → OSJ:4A (RÉSULTAT de l'AWB,
    #                   lecture seule).
    # gamma noir et détail ne s'appliquent que si leur mode dédié est actif (gamma manuel,
    # détail ON) : sinon la caméra ignore l'écriture — le garde-fou relecture le détecte.

    def _wb_mode(self):
        """Mode balance des blancs courant (entier OAW), ou None si illisible.
        NE JAMAIS écrire OAW : le mode est réglé au boîtier et un changement distant ne
        tient pas. On se contente de LIRE le mode pour router les commandes painting."""
        try:
            return int(_after(self._cam("QAW"), "OAW:"))
        except (DriverError, ValueError, TypeError):
            return None

    def _gain_rb_spec(self, mode):
        """{clé: (query, prefix)} du gain R/B selon le mode WB, ou None si non pilotable.
        AWC A/B (2/3) → OSJ:4B/4C ; VAR (9) → OSG:39/3A."""
        if mode in (2, 3):
            return {"color_gain_r": ("QSJ:4B", "OSJ:4B:"),
                    "color_gain_b": ("QSJ:4C", "OSJ:4C:")}
        if mode == 9:
            return {"color_gain_r": ("QSG:39", "OSG:39:"),
                    "color_gain_b": ("QSG:3A", "OSG:3A:")}
        return None

    def _paint_read(self, key, mode=None):
        if mode is None:
            mode = self._wb_mode()
        if key == "wb_color_temp":
            # VAR : consigne éditable (OSI:20) ; sinon : résultat de l'AWB (OSJ:4A).
            query, prefix = (("QSI:20", "OSI:20:") if mode == 9
                             else ("QSJ:4A", "OSJ:4A:"))
            raw = _after(self._cam(query), prefix)
            if not raw:
                return None
            try:
                return int(raw.split(":")[0][:5], 16)
            except ValueError:
                return None
        if key in ("color_gain_r", "color_gain_b"):
            spec = self._gain_rb_spec(mode)
            if not spec:
                return None
            raw = _after(self._cam(spec[key][0]), spec[key][1])
            if not raw:
                return None
            try:
                return int(raw, 16) - 0x800
            except ValueError:
                return None
        if key == "chroma_level":
            raw = _after(self._cam("QSD:B0"), "OSD:B0:")
            if not raw:
                return None
            try:
                return int(raw, 16)
            except ValueError:
                return None
        spec = {"gamma_black": ("QSI:3D", "OSI:3D:", 0x80),
                "detail_level": ("QSJ:15", "OSJ:15:", 0x80)}.get(key)
        if not spec:
            return None
        query, prefix, off = spec
        raw = _after(self._cam(query), prefix)
        if not raw:
            return None
        try:
            return int(raw, 16) - off
        except ValueError:
            return None

    def _paint_write(self, key, value):
        """Écriture painting UE, DÉPENDANTE DU MODE, avec VÉRIFICATION PAR RELECTURE.

        Envoie la commande adaptée au mode WB courant, laisse passer l'anti-rebond de la
        caméra (~0.35 s) puis relit : si la caméra a répondu une erreur (ERx) OU si la
        valeur relue diffère de la consigne, on lève une DriverError explicite. Un réglage
        gaté (gamma manuel, détail ON, balance manuelle) remonte ainsi une ERREUR claire
        dans le RCP au lieu d'un faux succès."""
        v = int(value)
        mode = self._wb_mode()
        if key == "wb_color_temp":
            if mode != 9:
                raise DriverError("température de couleur éditable uniquement en balance "
                                  "VAR — dans ce mode elle est le résultat de l'AWB "
                                  "(lecture seule)")
            cmd = "OSI:20:%05X:0" % max(0, min(0xFFFFF, v))
        elif key in ("color_gain_r", "color_gain_b"):
            spec = self._gain_rb_spec(mode)
            if not spec:
                raise DriverError("gain couleur non pilotable dans ce mode de balance des "
                                  "blancs (nécessite AWC A/B ou VAR)")
            cmd = "%s%03X" % (spec[key][1], (v + 0x800) & 0xFFF)
        elif key == "chroma_level":
            cmd = "OSD:B0:%02X" % (v & 0xFF)
        elif key == "gamma_black":
            cmd = "OSI:3D:%02X" % ((v + 0x80) & 0xFF)
        elif key == "detail_level":
            cmd = "OSJ:15:%02X" % ((v + 0x80) & 0xFF)
        else:
            raise Unsupported(f"painting non inscriptible : {key}")
        # Jusqu'à trois essais : la caméra a un anti-rebond qui peut ignorer une écriture
        # trop rapprochée. On ne retourne QUE si la relecture confirme la consigne — donc
        # jamais de faux succès. Un réglage réellement gaté échoue après les trois essais.
        res = ""
        for _ in range(3):
            try:
                res = self._cam(cmd)
            except DriverError:
                res = ""
            time.sleep(0.35)
            if self._paint_read(key, mode=mode) == v:
                return res
        raise DriverError("réglage refusé par la caméra — vérifier son mode "
                          "(ex. gamma manuel, détail ON, balance manuelle)")

    # Clés colorimétriques propres à la famille UE, dans l'ordre de lecture/écriture.
    _KNEE_KEYS = ("knee_point", "knee_slope")
    _PAINT_KEYS = ("wb_color_temp", "color_gain_r", "color_gain_b",
                   "gamma_black", "detail_level", "chroma_level")

    def _ue_color_params(self):
        """Params colorimétriques UE160 : knee + painting ÉDITABLES, mode-aware.

        La température et le gain R/B DÉPENDENT du mode de balance des blancs courant :
        en AWC A/B le gain est pilotable mais la température est le résultat de l'AWB
        (lecture seule) ; en VAR les deux sont éditables. On lit le mode une fois ici pour
        présenter l'inscriptibilité juste, sans mentir sur ce qui est pilotable."""
        mode = self._wb_mode()
        gain_ok = mode in (2, 3, 9)
        temp_ok = (mode == 9)
        temp_help = ("QSI:20 / OSI:20 — consigne Kelvin en balance VAR. Aller-retour validé "
                     "en direct sur AW-UE160." if temp_ok else
                     "QSJ:4A — température résultant de l'AWB : lecture seule dans ce mode "
                     "(éditable seulement en balance VAR).")
        gain_help = ("Gain couleur centré sur 0 (hex 0x800). AWC A/B → OSJ:4B/4C, "
                     "VAR → OSG:39/3A selon le mode courant. Aller-retour validé sur "
                     "AW-UE160." if gain_ok else
                     "Gain couleur non pilotable dans ce mode de balance des blancs "
                     "(nécessite AWC A/B ou VAR).")
        return [
            param("knee_point", "Knee (point)", "int", group="Knee", color=True,
                  writable=True, bulk=True, validated=True, min=80, max=109, step=1,
                  order=70, help="QSA:20 / OSA:20 — (code + 246) / 4. Aller-retour "
                                 "validé en direct sur AW-UE160."),
            param("knee_slope", "Knee (pente)", "int", group="Knee", color=True,
                  writable=True, bulk=True, validated=True, min=0, max=99, step=1,
                  order=71, help="QSA:24 / OSA:24 — code brut. Aller-retour validé "
                                 "en direct sur AW-UE160."),
            param("wb_color_temp", "Température de couleur", "int",
                  group="Balance des blancs", unit="K", color=True, writable=temp_ok,
                  bulk=temp_ok, validated=True, min=2000, max=15000, step=100,
                  order=72, help=temp_help),
            param("color_gain_r", "Gain couleur", "int", group="Balance des blancs",
                  color=True, writable=gain_ok, bulk=gain_ok, validated=True,
                  triplet="wbgain", channel="R", role="white", min=-200, max=200, step=1, big=10,
                  order=73, help=gain_help),
            param("color_gain_b", "Gain couleur", "int", group="Balance des blancs",
                  color=True, writable=gain_ok, bulk=gain_ok, validated=True,
                  triplet="wbgain", channel="B", role="white", min=-200, max=200, step=1, big=10,
                  order=74, help=gain_help),
            param("gamma_black", "Gamma (noir)", "int", group="Gamma", color=True,
                  writable=True, bulk=True, validated=True, min=-48, max=48, step=1,
                  order=75, help="QSI:3D / OSI:3D — offset centré sur 0 (hex 0x80). "
                                 "Nécessite le mode gamma manuel ; sinon l'écriture est "
                                 "refusée (garde-fou relecture)."),
            param("detail_level", "Niveau de détail", "int", group="Détail", color=True,
                  writable=True, bulk=True, validated=True, min=-48, max=48, step=1,
                  order=76, help="QSJ:15 / OSJ:15 — offset centré sur 0 (hex 0x80). "
                                 "Nécessite le détail activé ; sinon l'écriture est "
                                 "refusée (garde-fou relecture)."),
            param("chroma_level", "Chroma / saturation", "int", group="Matrice/Chroma",
                  color=True, writable=True, bulk=True, validated=True, min=0, max=255,
                  step=1, big=10, order=77,
                  help="QSD:B0 / OSD:B0 — niveau de chroma (octet brut). Indépendant du "
                       "mode de balance des blancs. Aller-retour validé sur AW-UE160."),
        ]

    def params_schema(self):
        fam = self.family()
        freq = self.freq()
        freqs = self.freq_labels()
        formats = [lbl for _c, lbl in _formats(fam, freq)] if freq else []

        # Pedestals colorimétriques, selon la génération (cf. _peds). HE : brut 096-centré,
        # 0-192, R/B. UE : signé 0-centré (hex), R/V/B (le vert en plus).
        peds, hx = self._peds()
        pmin, pmax = (-150, 150) if hx else (0, 192)
        phelp = ("OSJ:0F / OSG:4C-4E — hex centré (0 = neutre). Décodé sur AW-UE160." if hx
                 else "QTP / QRP / QBP — code brut (096 = neutre). Vérifié AW-HE130.")
        ped_params = [param("ped_master", "Pedestal maître", "int", group="Noir", color=True,
                            role="mblack", min=pmin, max=pmax, step=1, big=10, bulk=True,
                            validated=True, order=50, help="Pedestal général. " + phelp)]
        for ch, pk, lbl, od in (("R", "ped_r", "rouge", 51), ("V", "ped_g", "vert", 52),
                                ("B", "ped_b", "bleu", 53)):
            if pk in peds:
                ped_params.append(param(pk, "Pedestal " + lbl, "int", group="Noir", color=True,
                                        min=pmin, max=pmax, step=1, big=10, triplet="ped",
                                        channel=ch, role="black", bulk=True, validated=True,
                                        order=od, help="Pedestal " + lbl + ". " + phelp))
        return [
            param("power", "Alimentation", "bool", group="Général", bulk=True,
                  validated=True, help="Marche / veille (#O). Vérifié sur AW-HE130."),
            param("auto_focus", "Autofocus", "bool", group="Optique", bulk=True,
                  validated=True, help="#D1 (0 = manuel, 1 = auto). Vérifié sur AW-HE130."),
            param("preset_speed", "Vitesse de rappel des mémoires", "int", group="Mémoires",
                  min=0, max=999, bulk=True, validated=True,
                  help="#UPVS, plage 000–999. Vérifié sur AW-HE130."),

            # Lecture confirmée, ÉCRITURE inconnue → non inscriptibles. Les valeurs sont
            # les codes bruts de la caméra : la table code → valeur physique (dB, densité,
            # 1/x s) n'est pas établie, et l'inventer donnerait un affichage faux.
            param("gain", "Gain", "int", group="Exposition", color=True,
                  unit=("" if fam == FAMILY_HE else "dB"),
                  min=(0 if fam == FAMILY_HE else -3), max=(48 if fam == FAMILY_HE else 36),
                  step=1, big=(6 if fam == FAMILY_HE else 3),
                  writable=True, bulk=True, validated=True, order=30,
                  help=("QGU / OGU — code brut (correspondance dB à établir). Vérifié AW-HE130."
                        if fam == FAMILY_HE else
                        "QSL:25 / OSL:25 en dB (code = dB + 8). Vérifié AW-UE160.")),
            param("nd_filter", "Filtre ND (code brut)", "text", group="Image", writable=False,
                  validated=True, help="Lecture QFT. Correspondance code → densité à établir."),
            param("shutter", "Obturateur (code brut)", "text", group="Image", writable=False,
                  validated=True, help="Lecture QSH. Correspondance code → vitesse à établir."),
            param("zoom_pos", "Position zoom", "text", group="Optique", writable=False,
                  validated=True, help="Lecture #GZ (hexadécimal)."),

            # Le format vidéo reste le trou de la table : l'énumération exhaustive des 676
            # Format GÉNÉRAL. Inscriptible seulement si la table de la famille est connue
            # ET la fréquence lisible : sans les deux, traduire un libellé en code
            # reviendrait à deviner. `validated` distingue les deux familles — la liste de
            # choix est exacte sur HE (extraite de la caméra), inférée sur UE.
            param("video_format", "Format vidéo", "enum", group="Vidéo", output="",
                  writable=bool(formats), bulk=bool(formats),
                  options=[{"value": f, "label": f} for f in formats],
                  validated=bool(formats), order=20,
                  help=("QSA:87 / OSA:87 — vérifié en direct. Formats de la fréquence "
                        "courante (" + freqs.get(freq, "?") + ")."
                        + " Liste exacte, tirée du code de la caméra.")
                       if formats else
                       "Fréquence illisible : impossible de proposer des formats."),

            # Changer la fréquence FAIT REDÉMARRER la caméra (l'interface Panasonic prévoit
            # ~110 s) et invalide le format courant. Volontairement hors des actions
            # groupées : c'est une bascule de zone, pas un réglage d'exploitation.
            # `order` : la fréquence s'écrit AVANT le format. C'est la caméra qui l'impose,
            # et l'interface Panasonic elle-même le documente (« envoyer la fréquence
            # d'abord, le format ensuite ») : un format 50 Hz envoyé alors que la caméra
            # est encore en 59.94 Hz est refusé.
            param("video_freq", "Fréquence vidéo", "enum", group="Vidéo", bulk=False,
                  options=[{"value": v, "label": l} for v, l in freqs.items()],
                  validated=True, order=10, heavy=True,
                  help="QSE:77 / OSE:77. ATTENTION : la caméra redémarre (~2 min) et le "
                       "format vidéo est réinitialisé."),

            # --- Colorimétrie (paint). Le DÉTAIL (QDT/ODT) est commun HE et UE. Les PEDESTALS
            #     sont construits par génération dans `ped_params` (HE : R/B 096-centré ;
            #     UE : R/V/B 0-centré hex), rassemblés avec le « Noir » de la FR7. ---
            *ped_params,
            param("detail", "Détail", "bool", group="Détail", color=True,
                  bulk=True, validated=True, order=60,
                  help="QDT / ODT — netteté (détail) marche/arrêt. Vérifié AW-HE130."),
            # Colorimétrie propre à la famille UE : knee éditable + painting en lecture
            # seule (cf. _ue_color_params). Réservée à la UE, comme les pedestals hex.
            *(self._ue_color_params() if fam == FAMILY_UE else []),
        ] + [
            # Une ligne de tableau par sortie physique. En LECTURE SEULE : la commande
            # d'écriture est connue (OSJ:xx), mais la liste des formats admissibles dépend
            # du format système ET du mode de recadrage UHD. Proposer un choix sans cette
            # logique produirait des refus incompréhensibles à l'usage.
            self._out_param(key, query, pfx)
            for key, _lbl, query, pfx in OUTPUTS.get(fam, []) if key
        ]

    def _out_param(self, key, query, prefix):
        opts = self.out_options(key)
        return param(
            "video_format_" + key, "Format vidéo", "enum" if opts else "text",
            group="Vidéo", output=key, order=30, validated=True,
            writable=bool(opts), bulk=False,
            options=[{"value": lbl, "label": lbl} for _c, lbl in opts],
            help=(f"{query} / {prefix} — formats admis dans la configuration courante "
                  f"(format système, cadence et recadrage UHD compris)." if opts else
                  f"{query} — aucune option connue dans cette configuration : "
                  f"lecture seule."))

    def read_params(self, keys=None):
        """Une clé illisible vaut `None` (jamais 0, jamais une valeur par défaut) : l'UI
        doit pouvoir montrer « non lu » sans le confondre avec une vraie valeur."""
        want = set(keys) if keys else {p["key"] for p in self.params_schema()}
        out = {}
        if "power" in want:
            out["power"] = _try(lambda: self._ptz("#O").lower().endswith("1"))
        if "auto_focus" in want:
            out["auto_focus"] = _try(lambda: self._ptz("#D1").lower().endswith("1"))
        if "preset_speed" in want:
            out["preset_speed"] = _try(lambda: _int(_after(self._ptz("#UPVS"), "uPVS")))
        if "gain" in want:
            out["gain"] = _try(lambda: self._gain_read())
        if "nd_filter" in want:
            out["nd_filter"] = _try(lambda: _after(self._cam("QFT"), "OFT:"))
        if "shutter" in want:
            out["shutter"] = _try(lambda: _after(self._cam("QSH"), "OSH:"))
        if "zoom_pos" in want:
            out["zoom_pos"] = _try(lambda: _after(self._ptz("#GZ"), "gz"))
        for pk in self._peds()[0]:
            if pk in want:
                out[pk] = _try(lambda k=pk: self._ped_read(k))
        if "detail" in want:
            out["detail"] = _try(lambda: self._cam("QDT").strip().endswith("1"))
        if self.family() == FAMILY_UE:
            for which, pk in (("point", "knee_point"), ("slope", "knee_slope")):
                if pk in want:
                    out[pk] = _try(lambda w=which: self._knee_read(w))
            for pk in self._PAINT_KEYS:
                if pk in want:
                    out[pk] = _try(lambda k=pk: self._paint_read(k))
        if "video_freq" in want:
            out["video_freq"] = _try(lambda: self.freq(refresh=True))
        for key, _lbl, query, prefix in OUTPUTS.get(self.family(), []):
            pkey = "video_format" + (("_" + key) if key else "")
            if pkey in want:
                out[pkey] = _try(lambda q=query, pr=prefix: self._read_out_format(q, pr))
        return out

    def _read_out_format(self, query, prefix):
        """Format d'une sortie, traduit en libellé. `FF` = sortie inactive sur la UE160 :
        c'est une information, pas une lecture ratée — on ne la confond donc pas avec
        « non lu » (None), qui veut dire « la caméra n'a pas répondu »."""
        raw = _after(self._cam(query), prefix)
        if not raw:
            return None
        if raw.upper() == "FF":
            return "—"
        try:
            code = int(raw, 16)
        except ValueError:
            return raw
        return _format_label(self.family(), self.freq(), code) or f"{prefix}{raw}"


    def write_param(self, key, value):
        if key == "power":
            return self.power(bool(value))
        if key in ("ped_master", "ped_r", "ped_g", "ped_b"):
            return self._ped_write(key, value)
        if key == "gain":
            return self._gain_write(value)
        if key == "detail":
            return self._cam("ODT:" + ("1" if value else "0"))
        if key in ("knee_point", "knee_slope"):
            return self._knee_write("point" if key == "knee_point" else "slope", value)
        if key in self._PAINT_KEYS:
            # Painting UE : écriture mode-aware avec vérification par relecture.
            return self._paint_write(key, value)
        if key == "auto_focus":
            return self._ptz("#D1" + ("1" if value else "0"))
        if key == "preset_speed":
            return self._ptz("#UPVS%03d" % max(0, min(999, int(value))))
        if key == "video_format":
            fam, freq = self.family(), self.freq()
            if freq is None:
                raise DriverError("fréquence vidéo illisible : format non modifiable")
            code = _format_code(fam, freq, value)
            if code is None:
                raise DriverError(
                    f"format « {value} » indisponible en "
                    f"{self.freq_labels().get(freq, freq)} "
                    f"sur cette génération")
            return self._cam("OSA:87:%02X" % code)
        if key == "video_freq":
            v = str(value)
            if v not in self.freq_labels():
                raise DriverError(f"fréquence invalide : {value}")
            res = self._cam("OSE:77:" + v)
            self._freq = None            # la caméra redémarre : tout est à relire
            return res
        if key.startswith("video_format_"):
            out_key = key[len("video_format_"):]
            spec = next((o for o in OUTPUTS.get(self.family(), []) if o[0] == out_key), None)
            if not spec:
                raise Unsupported(f"sortie inconnue : {out_key}")
            code = next((c for c, lbl in self.out_options(out_key) if lbl == value), None)
            if code is None:
                raise DriverError(
                    f"format « {value} » non admis sur la sortie {spec[1]} dans la "
                    f"configuration courante")
            return self._cam(spec[3] + code)
        raise Unsupported(f"paramètre non inscriptible : {key}")

    # -- mémoires ------------------------------------------------------------

    def _wire(self, index):
        """Les mémoires sont numérotées 1..100 à l'affichage (comme sur les pupitres) mais
        00..99 sur le fil. Le décalage se fait ICI, une seule fois, jamais dans l'UI."""
        lo, hi = self.PRESET_RANGE
        i = int(index)
        if not lo <= i <= hi:
            raise DriverError(f"mémoire hors bornes : {i} (attendu {lo}..{hi})")
        return "%02d" % (i - 1)

    def presets(self):
        """Grille des mémoires. `used=None` partout : le protocole AW n'expose pas
        l'inventaire des mémoires occupées — « inconnu » n'est pas « libre ».

        Les NOMS sont lus dans la caméra quand elle sait les stocker (`QSJ:35`, présent
        sur AW-UE160, absent sur AW-HE130). Sinon la liste revient vide et le serveur
        superpose le nommage local."""
        lo, hi = self.PRESET_RANGE
        rows = [{"index": i, "name": "", "used": None} for i in range(lo, hi + 1)]
        if CAP_PRESET_NAMES not in self.capabilities():
            return rows
        names = self._read_preset_names(lo, hi)
        for r in rows:
            r["name"] = names.get(r["index"], "")
        return rows

    def _read_preset_names(self, lo, hi, workers=4):
        """Lit les noms de mémoires en parallèle borné.

        Une requête par mémoire : en série, cent allers-retours prendraient plusieurs
        secondes à chaque ouverture de l'onglet. Chaque fil a SA session HTTP — celle de
        l'instance n'est pas garantie sûre en multi-fils."""
        todo = list(range(lo, hi + 1))
        names, lock = {}, threading.Lock()

        def run():
            sess = requests.Session()
            while True:
                with lock:
                    if not todo:
                        return
                    i = todo.pop()
                wire = self._wire(i)
                try:
                    res = self._get("/cgi-bin/aw_cam",
                                    {"cmd": f"QSJ:35:{wire}", "res": "1"}, session=sess)
                except DriverError:
                    continue
                prefix = f"OSJ:35:{wire}:"
                if res.startswith(prefix):
                    # La caméra complète le nom par des espaces jusqu'à 15 caractères.
                    with lock:
                        names[i] = res[len(prefix):].rstrip()

        threads = [threading.Thread(target=run, daemon=True)
                   for _ in range(min(workers, max(1, len(todo))))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return names

    def rename_preset(self, index, name):
        """Écrit le nom DANS la caméra (`OSJ:35:<NN>:<nom>`) et renvoie le nom RÉELLEMENT
        écrit — celui-ci peut différer de la saisie, cf. `normalize_preset_name`.

        Un nom vide efface (`OSJ:36:<NN>`), ce qui rétablit le libellé d'usine. Si le
        modèle ne connaît pas la commande, on lève `Unsupported` : le serveur bascule alors
        sur le nommage local plutôt que de faire échouer le renommage.

        Pas de remplissage à 18 caractères, contrairement au JavaScript de la caméra : les
        espaces de complément font répondre « 400 Bad Request » à la CGI. Le bourrage n'a
        de sens que pour le transport interne de l'interface web."""
        if CAP_PRESET_NAMES not in self.capabilities():
            raise Unsupported("ce modèle ne mémorise pas de nom de mémoire")
        wire = self._wire(index)
        clean = normalize_preset_name(name)
        try:
            if not clean:
                self._cam("OSJ:36:" + wire)
                return ""
            self._cam(f"OSJ:35:{wire}:{clean}")
            return clean
        except DriverError as e:
            if "ER1" in str(e):
                raise Unsupported("ce modèle ne mémorise pas de nom de mémoire") from e
            raise

    def recall_preset(self, index):
        return self._ptz("#R" + self._wire(index))

    def store_preset(self, index):
        return self._ptz("#M" + self._wire(index))

    # -- divers --------------------------------------------------------------

    def power(self, on):
        return self._ptz("#O1" if on else "#O0")

    def snapshot(self):
        """Vignette JPEG. Le chemin diffère selon la génération : on essaie les deux."""
        for path, params in (
            ("/cgi-bin/camera", {"resolution": "640"}),
            ("/cgi-bin/camera", {"resolution": "640", "quality": "1"}),
        ):
            try:
                data = self._get(path, params, raw=True)
            except DriverError:
                continue
            if data[:2] == b"\xff\xd8":     # en-tête JPEG
                return data, "image/jpeg"
        raise Unsupported("pas de vignette JPEG sur ce modèle")

    def console(self, cmd, channel="ptz"):
        """Commande brute, réponse brute. Aucun filtrage : c'est l'outil de rétro-ingénierie.
        Le « # » manquant sur le canal tourelle est ajouté par confort."""
        cmd = (cmd or "").strip()
        if not cmd:
            raise DriverError("commande vide")
        if channel == "cam":
            res = self._get("/cgi-bin/aw_cam", {"cmd": cmd, "res": "1"})
        else:
            if not cmd.startswith("#"):
                cmd = "#" + cmd
            res = self._get("/cgi-bin/aw_ptz", {"cmd": cmd, "res": "1"})
        return {"cmd": cmd, "channel": channel, "response": res,
                "error": bool(_ERR.match(res))}

    def probe(self):
        out = []
        for channel, cmd, comment in PROBE:
            entry = {"channel": channel, "cmd": cmd, "comment": comment,
                     "response": "", "ok": False}
            try:
                res = (self._get("/cgi-bin/aw_cam", {"cmd": cmd, "res": "1"})
                       if channel == "cam"
                       else self._get("/cgi-bin/aw_ptz", {"cmd": cmd, "res": "1"}))
                entry["response"] = res
                entry["ok"] = bool(res) and not _ERR.match(res)
            except DriverError as e:
                entry["response"] = str(e)
            out.append(entry)
        return out


# --------------------------------------------------------------------------- utilitaires

_PRESET_NAME_OK = re.compile(r"[A-Za-z0-9_+]")


def normalize_preset_name(name):
    """Adapte un nom saisi à ce que la caméra sait réellement stocker.

    Contraintes MESURÉES sur AW-UE160, aucune n'est documentée :
      - 15 caractères maximum (16 → `ER1`) ;
      - **espace interdit** : la CGI répond « 400 Bad Request », y compris pour un espace
        au milieu du mot ;
      - **tiret interdit**, même réponse ;
      - les accents sont stockés SOUS FORME ENCODÉE (« Régie » ressort `R%C3%A9gie`),
        donc inutilisables tels quels.

    On translittère donc les accents (« Régie » → « Regie », qui passe) et on remplace tout
    caractère hors jeu sûr par « _ ». Le nom effectivement écrit est renvoyé à l'appelant
    et relu depuis la caméra juste après : l'utilisateur voit tout de suite ce qui a été
    retenu, plutôt qu'une saisie silencieusement transformée."""
    import unicodedata
    txt = unicodedata.normalize("NFKD", (name or "").strip())
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    out = "".join(c if _PRESET_NAME_OK.match(c) else "_" for c in txt)
    return out[:PRESET_NAME_MAX]


def _reason(exc):
    """Cause réseau en clair. Le message brut de `requests` (« HTTPConnectionPool(host=…,
    port=…): Max retries exceeded with url… ») est illisible dans un tableau d'exploitation
    et noie l'information utile : délai dépassé ou connexion refusée."""
    if isinstance(exc, requests.ConnectTimeout):
        return "délai de connexion dépassé (caméra éteinte ?)"
    if isinstance(exc, requests.ReadTimeout):
        return "pas de réponse dans le délai imparti"
    if isinstance(exc, requests.ConnectionError):
        return "connexion impossible (adresse, VLAN ou caméra hors tension)"
    return str(exc).split("\n")[0][:120]


def _after(res, prefix):
    """Partie utile d'une réponse `PRÉFIXE:valeur`. Tolérant à la casse et au préfixe absent."""
    if res is None:
        return None
    s = res.strip()
    if prefix and s.upper().startswith(prefix.upper()):
        return s[len(prefix):].strip()
    return s


def _int(s):
    try:
        return int(re.sub(r"[^0-9-]", "", s or ""))
    except ValueError:
        return None


def _try(fn):
    """Exécute une lecture ; renvoie `None` si la caméra ne sait pas répondre.
    Une lecture ratée ne doit jamais faire échouer la lecture des AUTRES paramètres."""
    try:
        return fn()
    except (DriverError, ValueError, TypeError):
        return None
