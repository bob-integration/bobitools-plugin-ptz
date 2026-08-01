# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Formats admissibles PAR SORTIE VIDÉO — famille AW-UE.

FICHIER GÉNÉRÉ à partir du code que la caméra sert elle-même
(`/js/pc/setting_signals.js`, fonctions `refresh12GSDISFPFormat`,
`refresh3GOutput1Format`, `refresh3GOutput2Format`, `refreshHDMIFormat`),
relevé sur une AW-UE160 réelle (firmware V2.48) le 2026-07-29.

Ce n'est pas une simple liste : les formats admis sur une sortie dépendent de QUATRE
variables, et se tromper d'une seule fait refuser la commande par la caméra (`ER2`) :

    fréquence système   QSE:77   0=59.94 · 1=50 · 2=24 · 3=23.98 · 4=60 Hz
    format système      QSA:87   code hexadécimal
    cadence (FPS)       QSL:DD   la branche « fps3 » ne vaut que si la valeur est 3
    recadrage UHD       QSJ:2E   la branche « crop2 » ne vaut que si la valeur est 2

Piège rencontré pendant l'extraction, à ne pas refaire : le JavaScript de la caméra
contient des branches **mises en commentaire**. Les prendre pour argent comptant produit
une table plausible mais fausse — c'est ce qui a fait refuser `1080/25PsF` sur le 12G
alors que la bonne réponse était `1080/50i`. Les commentaires sont retirés avant analyse.

Structure : SORTIE → fréquence → format système → variante → [(code, libellé)]
La variante « default » s'applique dès que ni `crop2` ni `fps3` ne sont remplies.
"""

# Commandes de contexte nécessaires à la résolution.
CTX_FPS = ("QSL:DD", "OSL:DD:")
CTX_UHD_CROP = ("QSJ:2E", "OSJ:2E:")

OUT_FORMATS = {
    "12g": {
        "0": {
            "01": {"default": [("01", "720/59.94p")]},
            "10": {"default": [("10", "1080/59.94p"), ("04", "1080/59.94i")]},
            "14": {"default": [("14", "1080/29.97p")]},
            "17": {"default": [("14", "1080/29.97p"), ("17", "2160/29.97p")]},
            "19": {"crop2": [("01", "720/59.94p"), ("19", "2160/59.94p")], "default": [("10", "1080/59.94p"), ("19", "2160/59.94p")]},
            "26": {"default": [("10", "1080/59.94p")]},
        },
        "1": {
            "02": {"default": [("02", "720/50p")]},
            "11": {"default": [("11", "1080/50p"), ("05", "1080/50i")], "fps3": [("11", "1080/50p"), ("08", "1080/25PsF")]},
            "15": {"default": [("15", "1080/25p")]},
            "18": {"default": [("15", "1080/25p"), ("18", "2160/25p")]},
            "1A": {"crop2": [("02", "720/50p"), ("1A", "2160/50p")], "default": [("11", "1080/50p"), ("1A", "2160/50p")]},
            "27": {"default": [("11", "1080/50p")]},
        },
        "2": {
            "22": {"default": [("22", "1080/24p")]},
        },
        "3": {
            "23": {"default": [("23", "1080/23.98p")]},
        },
    },
    "3g1": {
        "0": {
            "01": {"default": [("01", "720/59.94p")]},
            "10": {"default": [("10", "1080/59.94p"), ("04", "1080/59.94i")]},
            "14": {"default": [("14", "1080/29.97p")]},
            "17": {"default": [("14", "1080/29.97p")]},
            "19": {"crop2": [("01", "720/59.94p")], "default": [("10", "1080/59.94p"), ("04", "1080/59.94i")]},
            "26": {"default": [("10", "1080/59.94p")]},
        },
        "1": {
            "02": {"default": [("02", "720/50p")]},
            "11": {"default": [("11", "1080/50p"), ("05", "1080/50i")], "fps3": [("11", "1080/50p"), ("08", "1080/25PsF")]},
            "15": {"default": [("15", "1080/25p")]},
            "18": {"default": [("15", "1080/25p")]},
            "1A": {"crop2": [("02", "720/50p")], "default": [("11", "1080/50p"), ("05", "1080/50i")]},
            "27": {"default": [("11", "1080/50p")]},
        },
        "2": {
            "21": {"default": [("22", "1080/24p")]},
            "22": {"default": [("22", "1080/24p")]},
        },
        "3": {
            "23": {"default": [("23", "1080/23.98p")]},
        },
    },
    "3g2": {
        "0": {
            "01": {"default": [("01", "720/59.94p")]},
            "10": {"default": [("10", "1080/59.94p"), ("04", "1080/59.94i")]},
            "14": {"default": [("14", "1080/29.97p")]},
            "17": {"default": [("14", "1080/29.97p")]},
            "19": {"crop2": [("01", "720/59.94p")], "default": [("10", "1080/59.94p"), ("04", "1080/59.94i")]},
            "26": {"default": [("10", "1080/59.94p")]},
        },
        "1": {
            "02": {"default": [("02", "720/50p")]},
            "11": {"default": [("11", "1080/50p"), ("05", "1080/50i")], "fps3": [("11", "1080/50p"), ("08", "1080/25PsF")]},
            "15": {"default": [("15", "1080/25p")]},
            "18": {"default": [("15", "1080/25p")]},
            "1A": {"crop2": [("02", "720/50p")], "default": [("11", "1080/50p"), ("05", "1080/50i")]},
            "27": {"default": [("11", "1080/50p")]},
        },
        "2": {
            "21": {"default": [("22", "1080/24p")]},
            "22": {"default": [("22", "1080/24p")]},
        },
        "3": {
            "23": {"default": [("23", "1080/23.98p")]},
        },
    },
    "hdmi": {
        "0": {
            "01": {"default": [("01", "720/59.94p")]},
            "10": {"default": [("10", "1080/59.94p"), ("04", "1080/59.94i")]},
            "14": {"default": [("14", "1080/29.97p")]},
            "17": {"default": [("14", "1080/29.97p"), ("17", "2160/29.97p")]},
            "19": {"crop2": [("01", "720/59.94p"), ("19", "2160/59.94p")], "default": [("10", "1080/59.94p"), ("19", "2160/59.94p")]},
            "26": {"default": [("26", "1080/119.88p")]},
        },
        "1": {
            "02": {"default": [("02", "720/50p")]},
            "11": {"default": [("11", "1080/50p"), ("05", "1080/50i")]},
            "15": {"default": [("15", "1080/25p")]},
            "18": {"default": [("15", "1080/25p"), ("18", "2160/25p")]},
            "1A": {"crop2": [("02", "720/50p"), ("1A", "2160/50p")], "default": [("11", "1080/50p"), ("1A", "2160/50p")]},
            "27": {"default": [("27", "1080/100p")]},
        },
        "2": {
            "21": {"default": [("21", "2160/24p"), ("22", "1080/24p")]},
            "22": {"default": [("22", "1080/24p")]},
        },
        "3": {
            "23": {"default": [("23", "1080/23.98p")]},
        },
    },
}


# Formats SYSTÈME valides par fréquence, sur AW-UE160. Dérivés des `case` des mêmes
# fonctions : la caméra ne définit de sortie que pour les formats système qu'elle accepte.
# C'est ce qui explique le refus `ER3` sur `OSA:87:05` — `case "05"` est COMMENTÉ dans le
# code de la caméra, donc 1080/50i n'est pas un format système admissible ici, seulement
# un format de SORTIE.
SYS_FORMATS = {
    "0": [0x01, 0x10, 0x14, 0x17, 0x19, 0x26],
    "1": [0x02, 0x11, 0x15, 0x18, 0x1A, 0x27],
    "2": [0x21, 0x22],
    "3": [0x23],
}
