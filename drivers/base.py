# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Contrat commun à tous les pilotes de caméras tourelles.

L'outil est multi-marques : le serveur ne connaît QUE ce contrat, jamais le protocole
d'une marque. Ajouter Sony (VISCA over IP), Bird Dog, Canon… = déposer un module dans
`drivers/` qui sous-classe `PtzDriver` et s'enregistre via `@register`.

Deux principes qui gouvernent tout le reste :

1. **Aucune hypothèse de transport.** Panasonic parle HTTP/CGI, Sony parle UDP/VISCA.
   La classe de base n'impose donc ni `requests`, ni port HTTP, ni URL — seulement des
   méthodes métier. `host`/`port` restent volontairement opaques.

2. **Capacités déclarées, UI adaptative.** Une AW-HE130 ne sait pas nommer ses mémoires,
   une AW-UE160 si. Plutôt que de coder des exceptions par modèle dans l'UI, chaque pilote
   annonce ce qu'il sait faire (`capabilities()`), et le front n'affiche que ça. Une
   capacité ABSENTE n'est pas une erreur : c'est une fonction qu'on masque.

Les paramètres (format vidéo, gain, filtre ND…) ne sont pas une liste figée : chaque pilote
publie un SCHÉMA (`params_schema()`) décrivant ses paramètres — type, valeurs possibles,
groupe d'affichage, éligibilité au rappel groupé. Le front construit les contrôles à partir
de ce schéma, sans rien savoir de la marque.
"""

# --------------------------------------------------------------------------- capacités

CAP_POWER = "power"                  # marche / veille
CAP_PTZ = "ptz"                      # pilotage pan / tilt / zoom
CAP_PRESETS = "presets"              # mémoires : rappel / enregistrement
CAP_PRESET_NAMES = "preset_names"    # noms de mémoires STOCKÉS DANS LA CAMÉRA
CAP_PARAMS = "params"                # lecture / écriture de paramètres
CAP_SNAPSHOT = "snapshot"            # vignette JPEG
CAP_CONSOLE = "console"              # envoi de commande brute (mise au point)
CAP_NETWORK = "network"             # lecture / changement de l'adresse IP
CAP_TALLY = "tally"                  # état / lecture tally (rouge/vert), ex. CCU système Sony

# Catégories de matériel dans le parc (regroupement d'affichage). Le contrat est le même pour
# toutes ; c'est une caméra tourelle PTZ, une caméra système sur CCU, ou une simple fiche.
CAT_PTZ = "ptz"          # tourelle PTZ (Panasonic AW, Sony FR7…)
CAT_CCU = "ccu"          # caméra système pilotée par sa CCU (Sony 700/CNS…)
CAT_GENERIC = "generic"  # fiche non pilotée


class DriverError(Exception):
    """Échec métier d'un pilote (caméra injoignable, commande refusée, non supporté).

    Distinguée des exceptions Python ordinaires : le serveur la traduit en 502 propre
    avec le message tel quel, alors qu'une exception non prévue remonte en 500."""


class Unsupported(DriverError):
    """Le modèle (ou le pilote) ne sait pas faire ça. Non fatal : l'UI masque la fonction."""


# --------------------------------------------------------------------------- paramètres

def param(key, label, type_, group="Général", options=None, min=None, max=None,
          step=None, unit="", writable=True, bulk=False, validated=True, help="",
          order=50, heavy=False, output=None, color=False,
          big=None, triplet=None, channel=None, role=None):
    """Descripteur d'un paramètre pilotable, tel que consommé par le front.

    `bulk`     : le paramètre a du sens en RAPPEL GROUPÉ (appliquer la même valeur à
                 plusieurs caméras). Le format vidéo oui ; la position de zoom non.
    `validated`: la commande a été confirmée SUR DU MATÉRIEL RÉEL. Les tables de commandes
                 reconstituées depuis la documentation restent à `False` tant qu'on n'a pas
                 vérifié en direct ; l'UI les signale au lieu de laisser croire que c'est sûr.
    `order`    : ordre d'écriture lors d'une RESTAURATION, croissant. Certains paramètres
                 doivent précéder d'autres — chez Panasonic la fréquence vidéo doit être
                 écrite AVANT le format, sinon le format envoyé n'appartient plus à la zone
                 courante et la caméra le refuse. Sans cette notion, une restauration
                 correcte dépendrait de l'ordre des clés dans un dictionnaire.
    `heavy`    : l'écriture est une opération LOURDE (redémarrage, coupure du signal).
                 L'UI doit la faire confirmer à part au lieu de la noyer dans un lot.
    `output`   : rattache le paramètre à une SORTIE VIDÉO. Les caméras haut de gamme
                 (AW-UE160…) ont un réglage général PUIS un réglage par sortie (SDI, HDMI,
                 IP…) ; les modèles plus simples n'ont que le général. Plutôt que de coder
                 ce cas particulier dans l'UI, chaque pilote étiquette ses paramètres :
                   `None` → paramètre ordinaire, affiché dans la liste ;
                   `""`   → réglage GÉNÉRAL, première ligne du tableau des sorties ;
                   `"SDI 1"` → réglage de cette sortie-là, une ligne par sortie.
                 L'UI en construit un tableau. Une caméra sans sorties multiples produit
                 simplement un tableau à une ligne — aucun test de modèle nulle part.
    `color`    : le paramètre est un réglage COLORIMÉTRIQUE (paint : balance des blancs,
                 noir, gamma, détail, look…). L'onglet RCP rassemble ces paramètres de toutes
                 les caméras côte à côte ; ils restent aussi visibles dans l'onglet Paramètres.
    `big`      : pas « rapide » d'un bouton ±/molette (Alt/Maj). À défaut, l'UI prend 10×`step`.
    `triplet`  : identifiant de REGROUPEMENT des composantes R/V/B d'un même réglage sur une
                 seule ligne du RCP (ex. `"ped"` pour pedestal rouge + bleu). Les paramètres
                 partageant un même `triplet` sont rendus côte à côte, colorés par `channel`.
    `channel`  : composante colorée du triplet — `"R"`, `"G"`/`"V"` ou `"B"`. Fixe la couleur
                 et l'ordre du mini-contrôle dans la ligne du triplet.
    `role`     : RÔLE dans la disposition « pupitre » (RCP) : `"iris"` (diaph), `"mblack"`
                 (pedestal / master black), `"white"` (gain R/B des blancs, avec `channel`),
                 `"black"` (R/B des noirs, avec `channel`). Les réglages SANS rôle vont dans
                 l'« écran » du RCP (regroupés par `group`). Le front place chaque rôle à sa
                 place, sans rien savoir de la marque.
    """
    return {
        "key": key, "label": label, "type": type_, "group": group,
        "options": options or [], "min": min, "max": max, "step": step, "unit": unit,
        "writable": writable, "bulk": bulk, "validated": validated, "help": help,
        "order": order, "heavy": heavy, "output": output, "color": color,
        "big": big, "triplet": triplet, "channel": channel, "role": role,
    }


# --------------------------------------------------------------------------- registre

_DRIVERS = {}


def register(cls):
    """Décorateur d'enregistrement d'un pilote (clé = `cls.KIND`)."""
    _DRIVERS[cls.KIND] = cls
    return cls


def get_driver_class(kind):
    cls = _DRIVERS.get(kind)
    if not cls:
        raise DriverError(f"pilote inconnu : {kind}")
    return cls


def build(device, defaults=None):
    """Instancie le pilote d'une caméra du parc (dict du parc → objet pilote)."""
    d = defaults or {}
    cls = get_driver_class(device.get("driver"))
    return cls(
        host=device.get("host"),
        port=device.get("port") or cls.DEFAULT_PORT,
        user=device.get("user") or d.get("user") or "",
        password=device.get("password") or d.get("password") or "",
        model=device.get("model") or "",
        timeout=float(device.get("timeout") or 4),
    )


def catalog():
    """Catalogue des pilotes pour l'UI : marque, modèles connus, disponibilité."""
    out = []
    for kind, cls in sorted(_DRIVERS.items()):
        out.append({
            "kind": kind,
            "label": cls.LABEL,
            "brand": cls.BRAND,
            "category": cls.CATEGORY,
            "available": cls.AVAILABLE,
            "default_port": cls.DEFAULT_PORT,
            "needs_auth": cls.NEEDS_AUTH,
            "preset_range": list(cls.PRESET_RANGE),
            "models": cls.models(),
            "note": cls.NOTE,
        })
    return out


# --------------------------------------------------------------------------- classe de base

class PtzDriver:
    """Classe de base d'un pilote. Sous-classer, renseigner les attributs de classe,
    implémenter ce que la marque sait faire et laisser le reste lever `Unsupported`."""

    KIND = ""                  # identifiant technique, ex. "panasonic_aw"
    LABEL = ""                 # libellé UI, ex. "Panasonic (protocole AW)"
    BRAND = ""                 # marque, ex. "Panasonic"
    CATEGORY = CAT_PTZ         # famille de matériel (CAT_PTZ / CAT_CCU / CAT_GENERIC) — regroupement
    AVAILABLE = True           # False = pilote déclaré mais pas encore implémenté
    DEFAULT_PORT = 80
    NEEDS_AUTH = True
    PRESET_RANGE = (1, 100)    # bornes des mémoires, INCLUSIVES, telles qu'affichées
    NOTE = ""                  # avertissement affiché dans le formulaire d'ajout

    def __init__(self, host, port=None, user="", password="", model="", timeout=4):
        self.host = host
        self.port = port or self.DEFAULT_PORT
        self.user = user
        self.password = password
        self.model = model or ""
        self.timeout = timeout
        # Dernière erreur rencontrée. `reachable()` ne renvoie qu'un booléen ; sans ça, la
        # surveillance ne pourrait afficher que « sans réponse » là où la caméra a peut-être
        # répondu « authentification refusée » — deux pannes qui ne se dépannent pas pareil.
        self.last_error = None

    # -- catalogue -----------------------------------------------------------

    @classmethod
    def models(cls):
        """Modèles connus : [{value, label, family}]. `value` vide = « auto / inconnu »."""
        return [{"value": "", "label": "Auto (détecté à l'identification)", "family": ""}]

    # -- identité & disponibilité --------------------------------------------

    def reachable(self):
        """Test de disponibilité LÉGER (appelé en boucle par la surveillance de fond).
        Ne doit rien faire de coûteux : pas d'inventaire, pas de lecture de paramètres."""
        raise Unsupported("test de disponibilité non implémenté")

    def identify(self):
        """Identité relevée sur la caméra : {model, serial, firmware, mac, name}.
        Appelé à la demande (et une fois à l'ajout), jamais en boucle."""
        raise Unsupported("identification non implémentée")

    def capabilities(self):
        """Capacités effectives de CETTE caméra (dépend du modèle) : set de CAP_*."""
        return set()

    # -- paramètres ----------------------------------------------------------

    def params_schema(self):
        """Descripteurs des paramètres pilotables (cf. `param()`)."""
        return []

    def outputs(self):
        """Sorties vidéo de la caméra, dans l'ordre d'affichage : [{value, label}].

        `value` correspond au champ `output` des paramètres. La première entrée est le
        réglage général (`value` vide). Une caméra sans sorties multiples renvoie cette
        seule entrée — le tableau des sorties a alors une ligne, ce qui reste juste."""
        return [{"value": "", "label": "Réglage général"}]

    def read_params(self, keys=None):
        """Valeurs courantes : {key: value}. Une clé illisible vaut `None` — jamais une
        valeur inventée : l'UI doit pouvoir distinguer « nul » de « pas lu »."""
        raise Unsupported("lecture de paramètres non implémentée")

    def write_param(self, key, value):
        """Écrit UN paramètre. Lève `DriverError` si la caméra refuse."""
        raise Unsupported("écriture de paramètres non implémentée")

    # -- mémoires ------------------------------------------------------------

    def presets(self):
        """Mémoires : [{index, name, used}]. `name` vide si le modèle ne les nomme pas
        (le serveur superposera alors les noms tenus localement). `used` peut valoir
        `None` quand la caméra ne sait pas dire si une mémoire est occupée."""
        raise Unsupported("mémoires non implémentées")

    def recall_preset(self, index):
        raise Unsupported("rappel de mémoire non implémenté")

    def store_preset(self, index):
        raise Unsupported("enregistrement de mémoire non implémenté")

    def rename_preset(self, index, name):
        """Renomme la mémoire DANS la caméra. Lève `Unsupported` si le modèle ne sait pas :
        le serveur bascule alors sur le nommage local."""
        raise Unsupported("nommage des mémoires non supporté par ce modèle")

    # -- divers --------------------------------------------------------------

    def power(self, on):
        raise Unsupported("marche/veille non implémenté")

    def read_network(self):
        """Config réseau (ip4_addr, ip4_netmask, …). Seuls les pilotes qui l'exposent."""
        raise Unsupported("lecture réseau non supportée par ce modèle")

    def set_network(self, ip=None, netmask=None, gateway=None, dhcp=None):
        raise Unsupported("changement d'adresse non supporté par ce modèle")

    def snapshot(self):
        """Vignette : (bytes, content_type)."""
        raise Unsupported("vignette non implémentée")

    def console(self, cmd, channel=""):
        """Envoie une commande BRUTE et renvoie la réponse brute (mise au point / rétro-
        ingénierie). C'est l'outil qui permet de valider une table de commandes en direct."""
        raise Unsupported("console non implémentée")

    def probe(self):
        """Batterie de commandes de lecture candidates → réponses brutes.
        Sert à construire/corriger la table de commandes face à du matériel réel :
        [{cmd, channel, response, ok}]."""
        raise Unsupported("sondage non implémenté")
