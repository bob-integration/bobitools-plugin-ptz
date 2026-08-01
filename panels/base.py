# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Contrat commun à tous les pilotes de PUPITRES (télécommandes de caméras).

Un pupitre n'est pas une caméra : on ne lui lit pas des mémoires ni des paramètres image,
on lui pousse une TABLE D'AFFECTATION — quel numéro de caméra du pupitre pointe vers quelle
caméra réelle (IP, port, type de contrôle, identifiants). C'est pourquoi ce contrat est
DISTINCT de `drivers/base.py` (`PtzDriver`) plutôt qu'une capacité de plus : les deux ne
partagent presque rien.

La philosophie est la même que pour les caméras :

1. **Aucune hypothèse de transport ni de marque.** La classe de base n'impose ni HTTP, ni
   port, ni format. Un pupitre Panasonic parle CGI/Digest ; un autre parlerait autrement.

2. **Capacités déclarées.** Un pupitre annonce ce qu'il sait faire (`capabilities()`) et le
   serveur/UI n'expose que ça. Un modèle qui ne sait pas nommer ses caméras ne produit pas
   d'erreur : la fonction est simplement absente.

Ajouter une marque de pupitre = déposer un module dans `panels/` qui sous-classe
`PanelDriver` et s'enregistre via `@register`. Ni le serveur ni l'UI ne changent.
"""

# --------------------------------------------------------------------------- capacités

CAP_ASSIGN = "assign"            # lecture / écriture de la table d'affectation des caméras
CAP_USER_BUTTONS = "user_btn"    # affectation des touches USER à des fonctions
CAP_IDENTIFY = "identify"        # relevé d'identité (modèle, série, firmware)
CAP_MACROS = "macros"            # lecture / écriture / lecture des macros

# Types SÉMANTIQUES d'une étape de macro, indépendants de la marque. Le pilote traduit
# vers/depuis la commande native. `aw_raw`/`cgi_get`/`cgi_post` sont les échappatoires :
# toute commande qu'on ne sait pas décoder reste éditable et REJOUABLE telle quelle, donc
# une macro existante n'est jamais dénaturée par un aller-retour.
MACRO_RECALL_PRESET = "recall_preset"   # param = n° de mémoire (1..)
MACRO_RECALL_MACRO = "recall_macro"     # param = n° de macro (1..)
MACRO_WAIT_USER = "wait_user"           # pause jusqu'à action opérateur ; pas de param
MACRO_AW_RAW = "aw_raw"                 # param = commande AW brute
MACRO_CGI_GET = "cgi_get"               # param = chemin CGI (GET)
MACRO_CGI_POST = "cgi_post"             # param = chemin CGI (POST)
MACRO_EMPTY = "empty"                   # étape vide (place tenue)


def macro_step(index, type_=MACRO_EMPTY, cam="", param="", interval=0, raw=""):
    """Descripteur d'UNE étape de macro pour le front.

    `cam`      : n° de caméra AU PUPITRE (slot) concerné, ou "" (Wait/Recall Macro).
    `param`    : dépend du type (n° de mémoire, n° de macro, commande brute…).
    `interval` : délai en millisecondes après l'étape.
    `raw`      : commande native TELLE QUELLE (préservée pour un aller-retour sans perte)."""
    return {"index": index, "type": type_, "cam": cam, "param": param,
            "interval": interval, "raw": raw}


class PanelError(Exception):
    """Échec métier d'un pilote de pupitre (injoignable, refus, non supporté).

    Le serveur la traduit en 502 propre ; une exception imprévue remonte en 500."""


class Unsupported(PanelError):
    """Le modèle ne sait pas faire ça. Non fatal : l'UI masque la fonction."""


# --------------------------------------------------------------------------- affectation

# Types de contrôle d'un slot, tels que le matériel les code sur le fil.
CTRL_LAN = "1"        # caméra pilotée en IP (LAN)
CTRL_SERIAL = "2"     # caméra pilotée en série (RS-422…)
CTRL_NONE = "3"       # slot non affecté (NoAssign)

CTRL_LABELS = {
    CTRL_LAN: "LAN (IP)",
    CTRL_SERIAL: "Série",
    CTRL_NONE: "Non affecté",
}


def assignment(slot, control_type=CTRL_NONE, ip="", port="", user="", has_password=False):
    """Descripteur d'UN slot d'affectation, tel que consommé par le front.

    `slot`         : numéro de caméra AU PUPITRE (1..N), tel qu'affiché (C001 = slot 1).
    `control_type` : CTRL_LAN / CTRL_SERIAL / CTRL_NONE.
    `has_password` : booléen SEULEMENT — le mot de passe stocké dans le pupitre ne
                     redescend jamais au navigateur (même règle que pour les caméras).
    """
    return {
        "slot": slot,
        "control_type": control_type,
        "control_label": CTRL_LABELS.get(control_type, control_type),
        "ip": ip,
        "port": port,
        "user": user,
        "has_password": has_password,
    }


# --------------------------------------------------------------------------- registre

_PANELS = {}


def register(cls):
    """Décorateur d'enregistrement d'un pilote de pupitre (clé = `cls.KIND`)."""
    _PANELS[cls.KIND] = cls
    return cls


def get_panel_class(kind):
    cls = _PANELS.get(kind)
    if not cls:
        raise PanelError(f"pilote de pupitre inconnu : {kind}")
    return cls


def build(device, defaults=None):
    """Instancie le pilote d'un pupitre du parc (dict du parc → objet pilote)."""
    d = defaults or {}
    cls = get_panel_class(device.get("driver"))
    return cls(
        host=device.get("host"),
        port=device.get("port") or cls.DEFAULT_PORT,
        user=device.get("user") or d.get("user") or "",
        password=device.get("password") or d.get("password") or "",
        model=device.get("model") or "",
        timeout=float(device.get("timeout") or 6),
    )


def catalog():
    """Catalogue des pilotes de pupitres pour l'UI."""
    out = []
    for kind, cls in sorted(_PANELS.items()):
        out.append({
            "kind": kind,
            "label": cls.LABEL,
            "brand": cls.BRAND,
            "available": cls.AVAILABLE,
            "default_port": cls.DEFAULT_PORT,
            "needs_auth": cls.NEEDS_AUTH,
            "slot_count": cls.SLOT_COUNT,
            "models": cls.models(),
            "note": cls.NOTE,
        })
    return out


# --------------------------------------------------------------------------- classe de base

class PanelDriver:
    """Classe de base d'un pilote de pupitre. Sous-classer, renseigner les attributs de
    classe, implémenter ce que la marque sait faire, laisser le reste lever `Unsupported`."""

    KIND = ""                  # identifiant technique, ex. "panasonic_rp"
    LABEL = ""                 # libellé UI
    BRAND = ""                 # marque
    AVAILABLE = True           # False = pilote déclaré mais pas encore implémenté
    DEFAULT_PORT = 80
    NEEDS_AUTH = True
    SLOT_COUNT = 0             # nombre de slots caméra du pupitre (0 = inconnu/variable)
    MACRO_COUNT = 0            # nombre de macros (0 = non supporté)
    MACRO_MAX_STEPS = 0        # étapes maximum par macro
    NOTE = ""                  # avertissement affiché dans le formulaire d'ajout

    def __init__(self, host, port=None, user="", password="", model="", timeout=6):
        self.host = host
        self.port = port or self.DEFAULT_PORT
        self.user = user
        self.password = password
        self.model = model or ""
        self.timeout = timeout
        self.last_error = None

    # -- catalogue -----------------------------------------------------------

    @classmethod
    def models(cls):
        """Modèles connus : [{value, label}]. `value` vide = « auto / inconnu »."""
        return [{"value": "", "label": "Auto"}]

    # -- identité & disponibilité --------------------------------------------

    def reachable(self):
        """Test de disponibilité LÉGER (booléen). Renseigne `last_error` en cas d'échec."""
        raise Unsupported("test de disponibilité non implémenté")

    def identify(self):
        """Identité relevée : {model, serial, firmware, ...}. À la demande, jamais en boucle."""
        raise Unsupported("identification non implémentée")

    def capabilities(self):
        """Capacités effectives de CE pupitre : set de CAP_*."""
        return set()

    # -- affectation des caméras ---------------------------------------------

    def read_assignments(self):
        """Table d'affectation courante : liste de descripteurs `assignment(...)`, un par
        slot, dans l'ordre des slots. Le mot de passe n'est JAMAIS renvoyé (seul `has_password`)."""
        raise Unsupported("lecture des affectations non implémentée")

    def write_assignments(self, rows):
        """Écrit la table d'affectation. `rows` est la table ENTIÈRE (le matériel réécrit
        tout d'un bloc) : liste de dicts {slot, control_type, ip, port, user, password?}.
        Un mot de passe absent/None sur un slot signifie « conserver l'existant »."""
        raise Unsupported("écriture des affectations non implémentée")

    def assign(self, slot, ip=None, port=None, control_type=None, user=None, password=None):
        """Modifie UN slot par lecture-modification-réécriture de la table entière.
        Implémentation générique : les pilotes n'ont qu'à fournir read/write_assignments."""
        raise Unsupported("affectation unitaire non implémentée")

    # -- macros --------------------------------------------------------------

    def read_macro(self, index):
        """Étapes d'une macro : {index, label, steps:[macro_step(...)]}. `label` est un
        libellé d'affichage (« Macro 3 »)."""
        raise Unsupported("lecture des macros non implémentée")

    def write_macro(self, index, steps):
        """Écrit une macro ENTIÈRE. `steps` : liste de dicts {type, cam?, param?, interval?}.
        Remplace le contenu de la macro `index` (une macro n'a pas d'écriture d'étape unitaire
        côté matériel)."""
        raise Unsupported("écriture des macros non implémentée")

    def macro_control(self, index, play):
        """Joue (`play=True`) ou arrête (`play=False`) la macro `index`."""
        raise Unsupported("contrôle des macros non implémenté")
