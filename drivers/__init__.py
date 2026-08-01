# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Registre des pilotes de caméras.

Importer un module suffit à enregistrer son pilote (décorateur `@register`). Ajouter une
marque = déposer un module ici et l'importer ci-dessous ; ni le serveur ni l'UI ne changent.
"""
from .base import (  # noqa: F401 — ré-export : c'est l'API du paquet
    CAP_CONSOLE, CAP_PARAMS, CAP_POWER, CAP_PRESET_NAMES, CAP_PRESETS, CAP_PTZ,
    CAP_SNAPSHOT, DriverError, PtzDriver, Unsupported, build, catalog, get_driver_class,
    param, register,
)
from . import panasonic_aw  # noqa: F401 — l'import EST l'enregistrement
from . import sony_visca    # noqa: F401
from . import generic       # noqa: F401 — fiche non pilotée (autres marques)
