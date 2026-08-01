# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Registre des pilotes de PUPITRES (télécommandes de caméras).

Jumeau de `drivers/` mais pour les pupitres : importer un module suffit à enregistrer son
pilote (décorateur `@register`). Ajouter une marque = déposer un module ici et l'importer
ci-dessous ; ni le serveur ni l'UI ne changent.
"""
from .base import (  # noqa: F401 — ré-export : API du paquet
    CAP_ASSIGN, CAP_IDENTIFY, CAP_USER_BUTTONS, CTRL_LAN, CTRL_NONE, CTRL_SERIAL,
    PanelDriver, PanelError, Unsupported, assignment, build, catalog, get_panel_class,
    register,
)
from . import panasonic_rp  # noqa: F401 — l'import EST l'enregistrement
