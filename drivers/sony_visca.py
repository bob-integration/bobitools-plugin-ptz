# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Pilote Sony — VISCA over IP (UDP 52381).

Présent pour DÉMONTRER que le contrat de `base.py` ne suppose rien du transport : Panasonic
parle HTTP/CGI, Sony parle UDP binaire, et le serveur ne voit aucune différence.

La trame VISCA over IP est bien spécifiée, elle est donc implémentée ici :

    [type:2][longueur charge:2][n° séquence:4][charge VISCA…]

    type 0x0100 = commande/requête VISCA · 0x0200 = commande de contrôle
    charge      = 0x8x … 0xFF, où x = adresse caméra (1 en IP)

`AVAILABLE = False` : le code n'a JAMAIS tourné face à une caméra Sony. L'outil laisse
quand même ajouter une caméra Sony — l'UI signale simplement que le pilote n'est pas
validé. Le jour où une SRG/BRC est sur le réseau, les fonctions ci-dessous se vérifient
en quelques minutes et le drapeau passe à `True`.

Non implémenté volontairement : les paramètres (`params_schema`) — la table VISCA des
réglages image est vaste et la coder à l'aveugle produirait exactement le genre de
« plausible mais faux » qu'on cherche à éviter.
"""
import socket
import struct

from .base import (
    CAP_CONSOLE, CAP_POWER, CAP_PRESETS, CAP_PTZ, DriverError, PtzDriver, register,
)

PAYLOAD_COMMAND = 0x0100


@register
class SonyVisca(PtzDriver):
    KIND = "sony_visca"
    LABEL = "Sony (VISCA over IP)"
    BRAND = "Sony"
    AVAILABLE = False
    DEFAULT_PORT = 52381
    NEEDS_AUTH = False
    PRESET_RANGE = (1, 256)
    NOTE = ("Pilote NON VALIDÉ : écrit d'après la spécification VISCA, jamais confronté à "
            "une caméra Sony. À vérifier avant tout usage en exploitation.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._seq = 0

    @classmethod
    def models(cls):
        return [
            {"value": "", "label": "Auto / générique VISCA", "family": ""},
            {"value": "SRG-A40", "label": "Sony SRG-A40", "family": "srg"},
            {"value": "SRG-A12", "label": "Sony SRG-A12", "family": "srg"},
            {"value": "SRG-X400", "label": "Sony SRG-X400", "family": "srg"},
            {"value": "SRG-X120", "label": "Sony SRG-X120", "family": "srg"},
            {"value": "BRC-X400", "label": "Sony BRC-X400", "family": "brc"},
            {"value": "BRC-X1000", "label": "Sony BRC-X1000", "family": "brc"},
        ]

    # -- transport -----------------------------------------------------------

    def _send(self, payload, expect=True):
        """Émet une trame VISCA over IP et renvoie la charge utile de la réponse.

        UDP sans garantie de remise : un timeout n'est pas forcément une caméra absente,
        seulement une absence de réponse. Le message d'erreur le dit tel quel."""
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        header = struct.pack(">HHI", PAYLOAD_COMMAND, len(payload), self._seq)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(header + payload, (self.host, int(self.port)))
            if not expect:
                return b""
            data, _ = sock.recvfrom(1024)
        except socket.timeout as e:
            raise DriverError(f"{self.host}:{self.port} — pas de réponse VISCA") from e
        except OSError as e:
            raise DriverError(f"{self.host}:{self.port} — {e}") from e
        finally:
            sock.close()
        if len(data) < 8:
            raise DriverError("réponse VISCA tronquée")
        body = data[8:]
        # 0x60 = erreur de syntaxe, 0x61 = commande non exécutable
        if len(body) > 2 and body[1] & 0xF0 == 0x60:
            raise DriverError(f"commande refusée (code 0x{body[2]:02x})")
        return body

    # -- contrat -------------------------------------------------------------

    def reachable(self):
        self.last_error = None
        try:
            self._send(bytes([0x81, 0x09, 0x04, 0x00, 0xFF]))   # requête power
            return True
        except DriverError as e:
            self.last_error = str(e)
            return False

    def identify(self):
        """Requête « version inquiry » : 8x 09 00 02 FF → y0 50 gg gg hh hh jj jj kk FF,
        où hh hh est l'identifiant modèle. La correspondance code → nom commercial n'est
        pas publique : on renvoie le code brut plutôt qu'un nom inventé."""
        body = self._send(bytes([0x81, 0x09, 0x00, 0x02, 0xFF]))
        if len(body) < 9:
            raise DriverError("réponse d'identification inattendue")
        model_code = "%02X%02X" % (body[4], body[5])
        return {"model": self.model or f"VISCA:{model_code}", "firmware": "",
                "serial": "", "mac": "", "name": ""}

    def capabilities(self):
        return {CAP_POWER, CAP_PTZ, CAP_PRESETS, CAP_CONSOLE}

    def power(self, on):
        return self._send(bytes([0x81, 0x01, 0x04, 0x00, 0x02 if on else 0x03, 0xFF]))

    def _wire(self, index):
        lo, hi = self.PRESET_RANGE
        i = int(index)
        if not lo <= i <= hi:
            raise DriverError(f"mémoire hors bornes : {i} (attendu {lo}..{hi})")
        return i - 1        # affichage 1..256 → fil 0..255, comme chez Panasonic

    def presets(self):
        lo, hi = self.PRESET_RANGE
        return [{"index": i, "name": "", "used": None} for i in range(lo, hi + 1)]

    def recall_preset(self, index):
        return self._send(bytes([0x81, 0x01, 0x04, 0x3F, 0x02, self._wire(index), 0xFF]))

    def store_preset(self, index):
        return self._send(bytes([0x81, 0x01, 0x04, 0x3F, 0x01, self._wire(index), 0xFF]))

    def console(self, cmd, channel=""):
        """Trame VISCA saisie en hexadécimal, ex. « 81 09 04 00 FF »."""
        try:
            payload = bytes.fromhex((cmd or "").replace(",", " ").replace("0x", ""))
        except ValueError as e:
            raise DriverError(f"hexadécimal invalide : {e}") from e
        if not payload:
            raise DriverError("commande vide")
        body = self._send(payload)
        return {"cmd": payload.hex(" ").upper(), "channel": "visca",
                "response": body.hex(" ").upper(), "error": False}
