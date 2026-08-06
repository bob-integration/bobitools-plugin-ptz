# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Pilote « générique » : une caméra NON pilotée.

Toutes les caméras d'une presta ne sont pas des Panasonic AW. Pour en avoir malgré tout une
VUE GLOBALE (nom, adresse, numéro, joignabilité), on peut les déclarer en « générique » : une
simple fiche, sans aucune commande. Le type réel (BirdDog, Sony, Canon…) sera implémenté plus
tard, sous un pilote dédié — cette fiche générique n'y fait pas obstacle.

Une seule chose est faite en vrai : un test de joignabilité TCP léger (le port répond ou non),
histoire que la pastille d'état soit utile. Rien d'autre n'est deviné.
"""
import socket

from .base import CAT_GENERIC, PtzDriver, register


@register
class GenericDriver(PtzDriver):
    KIND = "generic"
    CATEGORY = CAT_GENERIC
    LABEL = "Générique (autre marque, non pilotée)"
    BRAND = "Générique"
    AVAILABLE = True
    DEFAULT_PORT = 80
    NEEDS_AUTH = False
    PRESET_RANGE = (1, 100)
    NOTE = ("Caméra NON pilotée : simple fiche (nom, adresse, numéro) pour figurer dans la "
            "vue globale de la presta. Aucune commande ; le type sera implémenté plus tard.")

    @classmethod
    def models(cls):
        return [{"value": "", "label": "Générique", "family": ""}]

    def reachable(self):
        """Joignabilité = le port TCP répond. On ne connaît pas le protocole, donc on ne teste
        que l'ouverture de la connexion (utile pour la pastille d'état, sans rien supposer)."""
        self.last_error = None
        s = socket.socket()
        s.settimeout(self.timeout)
        try:
            s.connect((self.host, self.port or self.DEFAULT_PORT))
            return True
        except Exception as e:                       # noqa: BLE001
            self.last_error = "%s injoignable : %s" % (self.host, e)
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass

    def identify(self):
        # Rien à relever de fiable sur une caméra dont on ignore le protocole.
        return {}

    def capabilities(self):
        return set()                                 # aucune commande : l'UI n'affiche rien
