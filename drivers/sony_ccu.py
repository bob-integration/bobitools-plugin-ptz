# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.
#
# CRÉDIT DE RÉTRO-INGÉNIERIE (obligatoire) :
#   Le protocole Sony 700 / CNS (« Camera Network System » / SPP, « Sony Proprietary
#   Protocol ») a été rétro-conçu par Oleksandr Nazaruk — Freehand (freehand.com.ua),
#   mail@freehand.com.ua. Ce module Python est un PORTAGE de ses deux dépôts publics,
#   tous deux sous GNU GPL v3 :
#     - freehand-dev/SONY.PTP700.SPP            (bibliothèque C# — transport, framing,
#                                                handshake, heartbeat, Message50, tests)
#     - DelphiForBroadcasting/sony-700ptp-protocol
#                                               (COMMANDADDR.pas : carte des adresses 700
#                                                pour le paint ; COMMANDHANDLERS.pas)
#   Rien n'a été deviné : chaque octet ci-dessous vient de ces sources. Le module n'ajoute
#   que l'adaptation au contrat `PtzDriver` de l'outil.

"""Pilote Sony — CCU système via protocole 700 / CNS (SPP) sur TCP:7700.

Cible : caméras système Sony pilotées par leur CCU (ex. HDCU-1500 / HDCU-2000 / série
1500), sur lesquelles on veut faire du **paint** (iris, noir, gains, balance des blancs…)
et lire le **tally** depuis un pupitre virtuel. C'est le pendant « caméra système » des
pilotes tourelles (Panasonic AW, Sony CGI) : même contrat, transport radicalement différent.

## Ce qui est PORTÉ FIDÈLEMENT (octet pour octet, depuis les sources Freehand)

- **Framing** `[header:u8][size:u8][payload:size]` avec chaînage de paquets successifs dans
  un même segment TCP (BasicPacket.InitPacket / NextPacket en C#). Vérifié hors ligne contre
  les paquets réels des tests XUnit (voir `_selftest()` en bas de module).
- **Types de paquet** (PacketHeader) : HandShake 0x01/0x02/0x03, Close 0x04/0x05,
  HeartBeat 0x08/0x09, Notify 0x0A/0x0B, Error 0x0C/0x0D, Message 0x0E, MessageResponse 0x0F.
- **Handshake** : disposition exacte de HandShake.cs (unknown_1 = 02 01 00, mode, id sur
  2 octets big-endian, unknown_1b = 01, device_type, device_type1, model, serial_number sur
  4 octets big-endian, unknown_2 = 64 32 0a 05), taille fixe 0x12. Les octets « unknown »
  sont repris tels quels de la lib. Réponse : header 0x03, on y lit model + serial + type.
- **Heartbeat** : paquet 0x08 émis après ~1 s d'inactivité, on ACK les 0x08 reçus par 0x09
  (et les Notify 0x0A par 0x0B), comme le fait CnsClient.
- **Message50** (« paint » MCS) : disposition exacte de Message50.cs — id, type=0x50,
  unknown=0x18, sub_type (Request 0x02 / Response 0x01), ccu_no (2o), unknown1=0x18,
  sender_srcid, ccu_no1 (2o), cmd_size (u16 big-endian), puis les paires de commande.
  Le champ ccu_no porte l'ADRESSAGE d'une CCU parmi plusieurs (MCS) — 1..10.
- **Carte des adresses paint** (`PAINT_ADDR`) : portée de COMMANDADDR.pas — pour chaque
  fonction, l'adresse requête `[groupe, param0]` et l'adresse réponse `[groupe, param0]`.
- **Encodage de valeur analogique** : d'après le commentaire de MicGainSelect.cs — un octet
  dont les bits 7-6 = contrôle (00 = valeur directe / interrogation, 01 = incrément,
  10 = décrément) et les bits 5-0 = valeur (0..63).

## Ce qui reste SUPPOSÉ / à VALIDER EN DIRECT (aucun matériel aujourd'hui)

Tout est marqué `validated=False`. À confirmer demain sur CCU réelle :

1. **Handshake réel** : les octets « unknown » sont ceux d'un pupitre RCP-1500 ; à vérifier
   qu'une CCU HDCU-1500 les accepte, et lire model/serial de la réponse.
2. **BRIDGE vs MCS** : en BRIDGE (1-à-1 réseau) on parle directement à l'IP de la CCU. En MCS
   (multi-caméras via MSU) il faut d'abord s'enregistrer (RCP no), demander la liste des CCU,
   obtenir l'IP de la CCU par `ccu_no`, puis se connecter. Le pilote implémente BRIDGE en dur
   et documente MCS ; le champ parc `ccu_number` porte l'adressage.
3. **Groupe REL vs ABS en écriture** : COMMANDADDR donne AddrReq (REL) et AddrRes (ABS).
   L'exemple mic-gain écrit en ABS ; on écrit donc en ABS et on interroge en REL+0x00. À
   confirmer que la CCU accepte bien un « set absolu » sur le groupe ABS.
4. **Plage/centre des valeurs paint** : 6 bits (0..63) bruts, sans supposer de point milieu
   signé — l'échelle réelle (ex. -99..+99 du pupitre) devra être calibrée en direct.
5. **Tally** : arrive en Notify (push). La disposition précise des octets rouge/vert n'est PAS
   décodée dans les sources (elles décodent surtout CAM_PW et l'assignation). Le pilote capte
   et conserve les Notify bruts et expose un crochet tally best-effort, à mapper en direct.
6. Adresses manquantes de COMMANDADDR (iris manuel, gamma, knee, detail, gains R/B de balance
   des blancs) : exposées en placeholder `addr=None`, à relever en direct avec la console.

## Infos réseau nécessaires demain

- IP d'ENTRÉE : en BRIDGE, l'IP de la CCU elle-même (port 7700). En MCS, l'IP du MSU.
- `ccu_number` (champ parc) : numéro de CCU (1..10) pour l'adressage MCS / le champ ccu_no.
- éventuellement le `rcp_number` (identité du pupitre virtuel) si le site en impose un.
"""
import socket
import struct
import threading
import time

try:
    from .base import (
        CAP_PARAMS, CAP_POWER, CAP_TALLY, CAT_CCU,
        DriverError, PtzDriver, Unsupported, param, register,
    )
except ImportError:  # exécution directe pour l'auto-test : `python3 drivers/sony_ccu.py`
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from base import (  # type: ignore
        CAP_PARAMS, CAP_POWER, CAP_TALLY, CAT_CCU,
        DriverError, PtzDriver, Unsupported, param, register,
    )

DEFAULT_PORT = 7700

# Numéro de série de l'ÉMULATEUR RCP que nous présentons à la CCU dans le handshake.
# CONFIRMÉ EN DIRECT (HSCU-1700, 2026-08-06) : une série NULLE est REJETÉE — la CCU renvoie
# un paquet d'en-tête 0x10 (NAK) puis ferme. Une série non nulle (ici celle de l'exemple
# AppBridge de Freehand) fait aboutir le handshake (réponse 0x03) et la session tient
# (heartbeat OK). Voir H_REJECT plus bas. Surchargeable par le champ parc `rcp_serial`.
DEFAULT_RCP_SERIAL = 0x0001c067

# --------------------------------------------------------------------------- protocole 700/CNS
# (porté de PacketFactory/BasicPacket.cs)

# En-têtes de paquet (PacketHeader)
H_HANDSHAKE_ACK = 0x01
H_HANDSHAKE = 0x02
H_HANDSHAKE_RESP = 0x03
H_CLOSE = 0x04
H_CLOSE_ACK = 0x05
H_HEARTBEAT = 0x08
H_HEARTBEAT_ACK = 0x09
H_NOTIFY = 0x0A
H_NOTIFY_ACK = 0x0B
H_ERROR_ID = 0x0C
H_ERROR = 0x0D
H_MESSAGE = 0x0E
H_MESSAGE_RESP = 0x0F
# En-tête OBSERVÉ EN DIRECT, ABSENT de l'enum de la lib Freehand (HSCU-1700, 2026-08-06) :
# la CCU l'émet pour REFUSER, soit un handshake à série nulle, soit — session établie — TOUT
# message de contrôle (Message10 d'état, Message50 de paint, SetActive) émis par un RCP NON
# AUTORISÉ/NON ASSIGNÉ. Le corps rejoue l'identité du handshake distant. Autrement dit : la
# session s'ouvre et la CCU s'identifie, mais le contrôle exige que ce RCP soit reconnu côté
# CCU (assignation caméra / passage par un MSU en MCS). Blocage EXTERNE, pas de framing.
H_REJECT = 0x10

# Modes de connexion CNS (CNSMode)
MODE_LEGACY = 0x00
MODE_BRIDGE = 0x01     # 1-à-1 réseau (une CCU, une IP) — implémenté ici
MODE_MCS = 0x02        # multi-caméras via MSU — documenté, adressage par ccu_no

# Identifiants de source (SRCID)
SRC_RCP = 0x90
SRC_HSCU = 0x40
SRC_MSU = 0x70
SRC_CHU = 0x20

# Modèles d'appareil connus dans le handshake (DeviceModel)
DEVICE_MODELS = {
    0x00: "CNA-1", 0x06: "MSU-1500", 0x0a: "RCP-1500",
    0x10: "HSCU-100", 0x0f: "HSCU-300", 0x1a: "HSCU-1700",
}

# Groupes de commande SPP (SppCommnadGroup) — utiles au paint
G_CHU_SWITCH_REL = 0x20
G_CHU_SWITCH_ABS = 0x21
G_CHU_ANALOG_REL = 0x22
G_CHU_ANALOG_ABS = 0x23
G_CCU_SWITCH_REL = 0x40
G_CCU_SWITCH_ABS = 0x41
G_CCU_ANALOG_REL = 0x42
G_CCU_ANALOG_ABS = 0x43

# Octets fixes du handshake (HandShake.cs) — repris VERBATIM de la lib Freehand.
_HS_UNKNOWN_1 = bytes([0x02, 0x01, 0x00])   # pos 0..2
_HS_UNKNOWN_1B = 0x01                        # pos 6
_HS_UNKNOWN_2 = bytes([0x64, 0x32, 0x0a, 0x05])  # pos 14..17
_HS_SIZE = 0x12                              # 18 octets de payload


# --------------------------------------------------------------------------- framing (testable hors ligne)

def encode_packet(header, payload=b""):
    """Encode un paquet 700 : [header:u8][size:u8][payload]. `size` = longueur du payload."""
    payload = bytes(payload)
    if len(payload) > 0xFF:
        raise DriverError("payload trop long pour le champ size (u8)")
    return bytes([header & 0xFF, len(payload)]) + payload


def iter_packets(buffer):
    """Découpe un segment TCP en paquets successifs (comme BasicPacket.NextPacket en C#).

    Rendu : liste de (header, payload). Le chaînage est purement séquentiel — chaque paquet
    consomme 2 + size octets ; ce qui suit est le paquet suivant. Sans dépendance au type."""
    out = []
    i = 0
    n = len(buffer)
    while i + 2 <= n:
        header = buffer[i]
        size = buffer[i + 1]
        if i + 2 + size > n:
            break  # paquet partiel : on garde le reste pour le prochain read
        payload = bytes(buffer[i + 2: i + 2 + size])
        out.append((header, payload))
        i += 2 + size
    return out, i  # i = nombre d'octets consommés


def build_handshake(header, mode, request_id, serial=0,
                    model=0x0b, device_type=SRC_RCP):
    """Construit un paquet handshake (HandShake.cs / InitHandShake).

    On se présente comme un RCP (device_type 0x90). Modèle 0x0b : CONFIRMÉ par capture d'un
    VRAI RCP dialoguant avec cette HSCU-1700 (le RCP annonce model 0x0b, pas 0x0a).

    PIÈGE CAPITAL révélé par la capture MITM (2026-08-06) : l'octet 1 d'`unknown_1` vaut 0x01
    dans le HANDSHAKE initial (`02 01 00`) mais **0x02 dans l'ACK** (`02 02 00`, miroir de la
    réponse de la CCU). Sans ce 0x02, la CCU tolère la session (heartbeats) mais REFUSE tout
    pilotage (NAK 0x10) — c'était LE bug qui bloquait le paint pendant des heures."""
    p = bytearray(_HS_SIZE)
    p[0:3] = _HS_UNKNOWN_1                       # unknown_1 = 02 01 00 …
    if header == H_HANDSHAKE_ACK:
        p[1] = 0x02                             # … mais 02 02 00 pour l'ACK (marqueur d'activation)
    p[3] = mode & 0xFF                           # cns mode
    struct.pack_into(">H", p, 4, request_id & 0xFFFF)  # id (big-endian)
    p[6] = _HS_UNKNOWN_1B                         # unknown_1b
    p[7] = device_type & 0xFF                     # device_type
    p[8] = device_type & 0xFF                     # device_type1 (idem RCP)
    p[9] = model & 0xFF                           # model
    struct.pack_into(">I", p, 10, serial & 0xFFFFFFFF)  # serial (big-endian)
    p[14:18] = _HS_UNKNOWN_2                       # unknown_2
    return encode_packet(header, p)


def parse_handshake(payload):
    """Extrait {mode, id, device_type, model, model_name, serial} d'un payload handshake."""
    if len(payload) < 14:
        raise DriverError("handshake trop court")
    mode = payload[3]
    request_id = struct.unpack_from(">H", payload, 4)[0]
    device_type = payload[7]
    model = payload[9]
    serial = struct.unpack_from(">I", payload, 10)[0]
    return {
        "mode": mode,
        "id": request_id,
        "device_type": device_type,
        "model": model,
        "model_name": DEVICE_MODELS.get(model, f"0x{model:02x}"),
        "serial": serial,
    }


# --------------------------------------------------------------------------- Message50 (paint MCS)
# Disposition (Message50.cs) — offsets DANS le payload (après header+size) :
#   id(0,2) type(2,1)=0x50 unknown(3,1)=0x18 sub_type(4,1) ccu_no(5,2)
#   unknown1(7,1)=0x18 sender_srcid(8,1) ccu_no1(9,2) cmd_size(11,2 BE) cmd_pairs(13,…)

M50_SUB_REQUEST = 0x02
M50_SUB_RESPONSE = 0x01
M50_SUB_ACTIVATE = 0x04   # sous-type porté par l'ACTIVATE (et les requêtes d'état/diagnostic)

# ACTIVATE (« bouton Activate » du RCP) — CAPTURÉ sur le fil d'un vrai RCP -> HSCU-1700 :
#   Message50 sub_type=0x04, ccu_no=0, deux paires 0x0b assignant le RCP (0x90) : voies 1 et 2.
# SANS cet envoi après le handshake, la CCU n'accepte AUCUN paint (elle laisse la session ouverte
# mais reste passive). C'est l'exact équivalent réseau du bouton Activate en face avant.
ACTIVATE_CMD = bytes([0x0b, 0x90, 0x01, 0x81, 0x0b, 0x90, 0x02, 0x81])

# Tailles des paires de commande par groupe (portées de Message50.SppCommands ToArray) :
#   SWITCH (marche/arrêt, 1 octet de valeur) = 3 o ; ANALOG (valeur 16 bits) = 4 o.
_PAIR3_GROUPS = {0x20, 0x21, 0x25, 0x40, 0x41, 0x60, 0x61}
_PAIR4_GROUPS = {0x0b, 0x22, 0x23, 0x27, 0x29, 0x3c, 0x3d, 0x42, 0x43, 0x49, 0x6c}


def parse_cmd_pairs(cmd_buffer):
    """Découpe un cmd_buffer en paires (group, param0, value). La valeur est l'octet unique
    (switch) ou l'entier 16 bits big-endian (analog), selon la taille de la paire du groupe.
    Best-effort : s'arrête proprement sur un groupe inconnu."""
    b = bytes(cmd_buffer)
    out, i, n = [], 0, len(b)
    while i + 2 <= n:
        g, p0 = b[i], b[i + 1]
        if g in _PAIR4_GROUPS or (g in (0x40, 0x41) and i + 1 < n and b[i + 1] == 0x0a):
            if i + 4 > n:
                break
            val = (b[i + 2] << 8) | b[i + 3]
            out.append((g, p0, val)); i += 4
        elif g in _PAIR3_GROUPS:
            if i + 3 > n:
                break
            out.append((g, p0, b[i + 2])); i += 3
        else:
            break
    return out


def build_message50(request_id, ccu_no, cmd_buffer, sub_type=M50_SUB_REQUEST,
                    sender_srcid=SRC_RCP):
    """Construit un paquet Message (0x0E) de type 0x50 portant `cmd_buffer` (octets des
    paires de commande) pour la CCU `ccu_no`. cmd_size = longueur du cmd_buffer (u16 BE)."""
    cmd_buffer = bytes(cmd_buffer)
    payload = bytearray(13 + len(cmd_buffer))
    struct.pack_into(">H", payload, 0, request_id & 0xFFFF)  # id
    payload[2] = 0x50                                        # type
    payload[3] = 0x18                                        # unknown
    payload[4] = sub_type & 0xFF                             # sub_type
    # ccu_no : l'octet bas porte le numéro (1..10). Sur le fil : [n, 00] (cf. CcuID setter).
    payload[5] = ccu_no & 0xFF
    payload[6] = 0x00
    payload[7] = 0x18                                        # unknown1
    payload[8] = sender_srcid & 0xFF                         # sender_srcid
    payload[9] = ccu_no & 0xFF                               # ccu_no1
    payload[10] = 0x00
    struct.pack_into(">H", payload, 11, len(cmd_buffer))     # cmd_size (BE)
    payload[13:] = cmd_buffer
    return encode_packet(H_MESSAGE, payload)


def parse_message50(payload):
    """Décompose un payload Message50 en champs + cmd_buffer brut.

    NB : le découpage des paires de commande individuelles est ambigu (une paire fait 2 à 5
    octets, la longueur dépend de la sémantique du groupe). On expose donc le cmd_buffer brut
    et `find_cmd()` pour y chercher une adresse précise — c'est ainsi que la lib fait ses
    lectures (Commands.Get par groupe+param0)."""
    if len(payload) < 13 or payload[2] != 0x50:
        raise DriverError("pas un Message50")
    cmd_size = struct.unpack_from(">H", payload, 11)[0]
    return {
        "id": struct.unpack_from(">H", payload, 0)[0],
        "sub_type": payload[4],
        "ccu_no": payload[5],
        "sender_srcid": payload[8],
        "cmd_size": cmd_size,
        "cmd_buffer": bytes(payload[13:13 + cmd_size]),
    }


def find_cmd(cmd_buffer, group, param0):
    """Cherche l'adresse [group, param0] dans un cmd_buffer et renvoie l'octet de valeur qui
    suit (PARAM1), ou None. Best-effort — utilisé pour lire une valeur paint en réponse."""
    b = bytes(cmd_buffer)
    i = 0
    while i + 2 < len(b):
        if b[i] == group and b[i + 1] == param0:
            return b[i + 2]
        i += 1
    return None


# --------------------------------------------------------------------------- encodage de valeur

def encode_analog_value(value):
    """Octet de valeur analogique (MicGainSelect.cs) : bits7-6 = 00 (valeur directe),
    bits5-0 = valeur 0..63. La calibration réelle (échelle pupitre) reste à faire en direct."""
    return value & 0x3F


# Incrément / décrément d'après le COMMENTAIRE de MicGainSelect.cs (source de vérité
# documentaire) : bits7-6 = 01 → incrément, 10 → décrément. NB : l'enum du même fichier
# nomme Inc=0x80 / Dec=0x40, ce qui CONTREDIT le commentaire — divergence à trancher en
# direct. On suit ici le commentaire.
INC_CTRL = 0x40   # 01xxxxxx
DEC_CTRL = 0x80   # 10xxxxxx
QUERY_STATUS = 0x00


# --------------------------------------------------------------------------- carte des adresses paint
# Portée VERBATIM de FH.SONY.SPP.COMMANDADDR.pas. Pour chaque clé plugin :
#   req = (groupe, param0) adresse de REQUÊTE (REL)  — interrogation
#   res = (groupe, param0) adresse de RÉPONSE (ABS)  — valeur retournée / écriture absolue
#   kind = "switch" (marche/arrêt) | "analog" (valeur) | None (adresse inconnue, à relever)
# Tout est validé=False : aucune n'a été confirmée en direct.

PAINT_ADDR = {
    # -- interrupteurs (switch) : marche/arrêt ; certains PARTAGENT un registre (bits) --
    "bars":        {"req": (0x40, 0x10), "res": (0x41, 0x10), "kind": "switch",
                    "note": "barres de couleur (bit0 du registre 0x10 ; partagé avec chroma)"},
    "chroma":      {"req": (0x40, 0x10), "res": (0x41, 0x10), "kind": "switch",
                    "note": "coupe la chroma VBS (bit1 du registre 0x10 ; partagé avec bars)"},
    "cam_power":   {"req": (0x40, 0x11), "res": (0x41, 0x11), "kind": "switch",
                    "note": "alimentation tête caméra depuis la CCU (CAM PW)"},
    "atw":         {"req": (0x20, 0x84), "res": (0x21, 0x84), "kind": "switch",
                    "note": "Auto Tracing White balance ON"},
    "master_gain": {"req": (0x20, 0x01), "res": (0x21, 0x01), "kind": "switch",
                    "note": "gain maître (bouton, pas à pas)"},
    "nd_filter":   {"req": (0x20, 0x03), "res": (0x21, 0x03), "kind": "switch",
                    "note": "filtre ND"},
    "cc_filter":   {"req": (0x20, 0x04), "res": (0x21, 0x04), "kind": "switch",
                    "note": "filtre CC (température)"},
    # -- valeurs analogiques (analog) : noir, flare, black-set --
    "black_master": {"req": (0x22, 0xA9), "res": (0x23, 0xA9), "kind": "analog",
                     "note": "master black"},
    "black_r":     {"req": (0x22, 0xAA), "res": (0x23, 0xAA), "kind": "analog", "note": "black R"},
    "black_g":     {"req": (0x22, 0xAB), "res": (0x23, 0xAB), "kind": "analog", "note": "black G"},
    "black_b":     {"req": (0x22, 0xAC), "res": (0x23, 0xAC), "kind": "analog", "note": "black B"},
    "black_set_r": {"req": (0x22, 0x80), "res": (0x23, 0x80), "kind": "analog", "note": "black set R"},
    "black_set_g": {"req": (0x22, 0x81), "res": (0x23, 0x81), "kind": "analog", "note": "black set G"},
    "black_set_b": {"req": (0x22, 0x82), "res": (0x23, 0x82), "kind": "analog", "note": "black set B"},
    "flare_master": {"req": (0x22, 0x08), "res": (0x23, 0x08), "kind": "analog", "note": "flare master"},
    "flare_r":     {"req": (0x22, 0x09), "res": (0x23, 0x09), "kind": "analog", "note": "flare R"},
    "flare_g":     {"req": (0x22, 0x0A), "res": (0x23, 0x0A), "kind": "analog", "note": "flare G"},
    "flare_b":     {"req": (0x22, 0x0B), "res": (0x23, 0x0B), "kind": "analog", "note": "flare B"},
    # -- adresses CONFIRMÉES par capture MITM (2026-08-06) + table de noms C# --
    "iris":        {"req": (0x22, 0x60), "res": (0x23, 0x60), "kind": "analog",
                    "note": "iris/diaphragme (16 bits, capté : 0x406b→0x7fff)"},
    "wb_gain_r":   {"req": (0x22, 0x01), "res": (0x23, 0x01), "kind": "analog", "note": "white R (gain)"},
    "wb_gain_g":   {"req": (0x22, 0x02), "res": (0x23, 0x02), "kind": "analog", "note": "white G (gain)"},
    "wb_gain_b":   {"req": (0x22, 0x03), "res": (0x23, 0x03), "kind": "analog", "note": "white B (gain)"},
    "gamma":       {"req": (0x22, 0x1C), "res": (0x23, 0x1C), "kind": "analog", "note": "master gamma"},
    "gamma_r":     {"req": (0x22, 0x1D), "res": (0x23, 0x1D), "kind": "analog", "note": "gamma R"},
    "gamma_g":     {"req": (0x22, 0x1E), "res": (0x23, 0x1E), "kind": "analog", "note": "gamma G"},
    "gamma_b":     {"req": (0x22, 0x1F), "res": (0x23, 0x1F), "kind": "analog", "note": "gamma B"},
    "knee_point":  {"req": (0x22, 0x14), "res": (0x23, 0x14), "kind": "analog", "note": "master knee point"},
    "knee_slope":  {"req": (0x22, 0x18), "res": (0x23, 0x18), "kind": "analog", "note": "master knee slope"},
    "detail":      {"req": (0x22, 0x9B), "res": (0x23, 0x9B), "kind": "analog", "note": "detail level"},
    "white_clip":  {"req": (0x22, 0x20), "res": (0x23, 0x20), "kind": "analog", "note": "master white clip"},
    "saturation":  {"req": (0x22, 0xD2), "res": (0x23, 0xD2), "kind": "analog", "note": "saturation"},
    "master_wgain":{"req": (0x22, 0xF2), "res": (0x23, 0xF2), "kind": "analog", "note": "master white gain"},
    # matrice linéaire
    "mtx_gr_r":    {"req": (0x22, 0xA3), "res": (0x23, 0xA3), "kind": "analog", "note": "matrice G-R sur R"},
    "mtx_br_r":    {"req": (0x22, 0xA4), "res": (0x23, 0xA4), "kind": "analog", "note": "matrice B-R sur R"},
    "mtx_rg_g":    {"req": (0x22, 0xA5), "res": (0x23, 0xA5), "kind": "analog", "note": "matrice R-G sur G"},
    "mtx_bg_g":    {"req": (0x22, 0xA6), "res": (0x23, 0xA6), "kind": "analog", "note": "matrice B-G sur G"},
    "mtx_rb_b":    {"req": (0x22, 0xA7), "res": (0x23, 0xA7), "kind": "analog", "note": "matrice R-B sur B"},
    "mtx_gb_b":    {"req": (0x22, 0xA8), "res": (0x23, 0xA8), "kind": "analog", "note": "matrice G-B sur B"},
}

# Listes de param0 interrogés par le vrai RCP lors du rituel de connexion (capturées, toutes
# valides — un param0 invalide fait renvoyer une ErrorID et peut faire fermer la CCU).
RITUAL_G40 = [0x00, 0x03, 0x04, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x10, 0x11, 0x12, 0x13, 0x14,
              0x15, 0x16, 0x19, 0x1a, 0x20, 0xe3, 0xe4, 0xe5, 0xe6, 0xf0, 0xf1]
RITUAL_G20 = [0x00, 0x01, 0x03, 0x04, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0c, 0x0d, 0x0e, 0x0f,
              0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x1a, 0x1d, 0x20, 0x26, 0x27, 0x28, 0x29,
              0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x30, 0x31, 0x32, 0x33, 0x35]

# Échelle d'AFFICHAGE des réglages paint signés : le pupitre Sony montre ±99, la valeur interne
# est sur 16 bits signés (±32767). CONFIRMÉ EN DIRECT : interne −4368 ↔ pupitre −13.
# L'iris est EXCLU (échelle absolue propre, rendu en F-stop). display = round(raw·99/32767).
PAINT_FULL = 32767
PAINT_DISP = 99


def raw_to_disp(raw):
    # TRONCATURE vers zéro (comme le pupitre Sony) : il faut un pas complet pour quitter 0, sinon
    # on afficherait ±1 près de zéro là où le RCP montre encore 0 (arrondi au plus proche = faux).
    return int(raw * PAINT_DISP / PAINT_FULL)


def disp_to_raw(disp):
    # on vise le HAUT du palier d'affichage (+ ~0.5 pas) pour que l'écriture se relise à l'identique
    # malgré la troncature en lecture (aller-retour ±99 ↔ 16 bits stable).
    if disp == 0:
        return 0
    step = PAINT_FULL / PAINT_DISP
    raw = int(round(disp * step + (0.5 * step if disp > 0 else -0.5 * step)))
    return max(-PAINT_FULL, min(PAINT_FULL, raw))


def _scaled(key, spec):
    """Un réglage est affiché en ±99 s'il est analogique ET n'est pas l'iris (échelle propre)."""
    return spec.get("kind") == "analog" and key != "iris"


# --------------------------------------------------------------------------- moteur de connexion

class CnsEngine:
    """Moteur protocole 700/CNS : socket TCP:7700 persistant, thread de fond (réception +
    heartbeat), handshake, envoi Request → attente Response, réception Notify (→ callback).

    Porté de CnsClient(.cs) / CnsClient.Sender / CnsClient.Receiver. Volontairement sans
    dépendance à l'outil : c'est du transport pur, réutilisable et testable."""

    HEARTBEAT_AFTER = 1.0   # comme CnsSender : heartbeat après ~1 s d'inactivité

    def __init__(self, host, port=DEFAULT_PORT, mode=MODE_BRIDGE, timeout=4.0,
                 serial=0, notify_cb=None):
        self.host = host
        self.port = port
        self.mode = mode
        self.timeout = timeout
        self.serial = serial
        self.notify_cb = notify_cb          # appelé (header, payload) pour chaque Notify
        self._sock = None
        self._lock = threading.RLock()       # sérialise l'écriture socket
        self._thread = None
        self._stop = threading.Event()
        self._last_tx = 0.0
        self._request_id = 0x6468            # valeur initiale de la lib (RCP)
        self.remote = None                   # infos handshake distant {model_name, serial…}
        self._pending = {}                   # response_id -> (Event, [packet])
        self._recv_buf = bytearray()

    # -- identifiants ------------------------------------------------------
    def next_request_id(self):
        rid = self._request_id
        self._request_id = (self._request_id + 1) & 0xFFFF
        return rid

    # -- cycle de vie ------------------------------------------------------
    def connect(self):
        self.disconnect()
        self._stop.clear()
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise DriverError(f"{self.host}:{self.port} injoignable : {e}") from e
        self._sock.settimeout(0.2)
        try:
            self._handshake()
        except Exception:
            self.disconnect()
            raise
        self._thread = threading.Thread(target=self._run, name="cns-recv", daemon=True)
        self._thread.start()

    def disconnect(self, graceful=True):
        self._stop.set()
        s = self._sock
        if s is not None:
            try:
                if graceful and self.remote:
                    s.sendall(encode_packet(H_CLOSE))
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        self._sock = None
        self.remote = None
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    @property
    def connected(self):
        return self._sock is not None and self.remote is not None

    # -- handshake (synchrone, avant le thread) ----------------------------
    def _handshake(self):
        # PEEK de l'id SANS l'incrémenter : le handshake, l'ACK ET le 1er message de contrôle
        # partagent le MÊME id (comportement exact du vrai RCP — le C# lit le champ _requestID
        # sans l'avancer). Si on incrémente ici, la CCU voit un trou dans la séquence et renvoie
        # une ErrorID (0x0c) sur TOUT message → le lien tête caméra ne s'établit jamais.
        rid = self._request_id
        self._raw_send(build_handshake(H_HANDSHAKE, self.mode, rid, self.serial))
        payload = self._read_one_packet(expect_header=H_HANDSHAKE_RESP)
        info = parse_handshake(payload)
        # ACK — même id, unknown_1 = 02 02 00 (géré dans build_handshake pour H_HANDSHAKE_ACK)
        self._raw_send(build_handshake(H_HANDSHAKE_ACK, self.mode, rid, self.serial))
        self.remote = info
        return info

    def _read_one_packet(self, expect_header=None, deadline=None):
        """Lit UN paquet complet du socket (utilisé pendant le handshake, hors thread)."""
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        while True:
            packets, consumed = iter_packets(self._recv_buf)
            if packets:
                del self._recv_buf[:consumed]
                header, payload = packets[0]
                if expect_header is not None and header != expect_header:
                    raise DriverError(
                        f"handshake : en-tête inattendu 0x{header:02x} "
                        f"(attendu 0x{expect_header:02x})")
                # remettre d'éventuels paquets suivants ? (rare au handshake) — ignorés ici
                return payload
            if time.monotonic() > deadline:
                raise DriverError("délai dépassé en attente d'un paquet")
            try:
                chunk = self._sock.recv(1024)
            except socket.timeout:
                continue
            except OSError as e:
                raise DriverError(f"lecture socket : {e}") from e
            if not chunk:
                raise DriverError("connexion fermée par la CCU")
            self._recv_buf += chunk

    # -- boucle de fond ----------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            # heartbeat
            if time.monotonic() - self._last_tx > self.HEARTBEAT_AFTER:
                try:
                    self._raw_send(encode_packet(H_HEARTBEAT))
                except DriverError:
                    break
            # réception
            try:
                chunk = self._sock.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self._recv_buf += chunk
            packets, consumed = iter_packets(self._recv_buf)
            if consumed:
                del self._recv_buf[:consumed]
            for header, payload in packets:
                self._dispatch(header, payload)

    def _dispatch(self, header, payload):
        if header == H_HEARTBEAT:
            self._safe_send(encode_packet(H_HEARTBEAT_ACK))
        elif header == H_HEARTBEAT_ACK:
            pass
        elif header == H_NOTIFY:
            self._safe_send(encode_packet(H_NOTIFY_ACK))
            if self.notify_cb:
                try:
                    self.notify_cb(header, payload)
                except Exception:
                    pass
        elif header == H_CLOSE:
            self._safe_send(encode_packet(H_CLOSE_ACK))
            self._stop.set()
        elif header == H_MESSAGE_RESP:
            # payload = [id:2][ ...paquet imbriqué éventuel ]
            if len(payload) >= 2:
                resp_id = struct.unpack_from(">H", payload, 0)[0]
                waiter = self._pending.get(resp_id)
                if waiter:
                    waiter[1].append(payload)
                    waiter[0].set()
        elif header == H_MESSAGE:
            # Message spontané (statut, tally imbriqué). La CCU ATTEND un accusé : le client C#
            # répond systématiquement par une MessageResponse (0x0F, id+1). CONFIRMÉ EN DIRECT
            # (HSCU-1700) : sans cet accusé la CCU finit par fermer ; avec, la session tient.
            if len(payload) >= 2:
                mid = struct.unpack_from(">H", payload, 0)[0]
                self._safe_send(encode_packet(H_MESSAGE_RESP,
                                              struct.pack(">H", (mid + 1) & 0xFFFF)))
            if self.notify_cb:
                try:
                    self.notify_cb(header, payload)
                except Exception:
                    pass

    # -- émission ----------------------------------------------------------
    def _raw_send(self, data):
        with self._lock:
            if self._sock is None:
                raise DriverError("socket fermé")
            try:
                self._sock.sendall(data)
            except OSError as e:
                raise DriverError(f"écriture socket : {e}") from e
            self._last_tx = time.monotonic()

    def _safe_send(self, data):
        try:
            self._raw_send(data)
        except DriverError:
            pass

    def request(self, request_id, packet, timeout=1.0):
        """Envoie un paquet Message et attend la MessageResponse corrélée.

        La lib corrèle la réponse sur `request_id + 1` (SubscribeResponse). On souscrit AVANT
        d'émettre pour ne pas rater une réponse rapide."""
        resp_id = (request_id + 1) & 0xFFFF
        ev = threading.Event()
        box = []
        self._pending[resp_id] = (ev, box)
        try:
            self._raw_send(packet)
            ev.wait(timeout)
        finally:
            self._pending.pop(resp_id, None)
        return box[0] if box else None


# --------------------------------------------------------------------------- pilote

MODELS = [
    ("", "Auto (détecté à l'identification)"),
    ("HDCU-1500", "Sony HDCU-1500 (CCU système)"),
    ("HDCU-2000", "Sony HDCU-2000 (CCU système)"),
    ("HDCU-2500", "Sony HDCU-2500 (CCU système)"),
    ("HSCU-1700", "Sony HSCU-1700 (CCU système)"),
    ("HXCU-100", "Sony HXCU-100 (CCU système)"),
]


# --------------------------------------------------------------------------- sessions persistantes
# Une CCU = UNE session TCP 700/CNS (BRIDGE 1:1), coûteuse à établir (handshake + rituel tête
# caméra). Le serveur crée une instance de pilote FRAÎCHE à chaque requête web : sans partage, chaque
# réglage relancerait tout le rituel (~2-3 s de latence). On garde donc les moteurs vivants, indexés
# par (host, port), réutilisés entre requêtes ; le heartbeat du moteur maintient la session ouverte.
# L'état paint poussé par la tête caméra est stocké SUR le moteur (partagé entre instances).
_ENGINES = {}
_ENGINES_LOCK = threading.Lock()


@register
class SonyCcu(PtzDriver):
    KIND = "sony_ccu"
    LABEL = "Sony (CCU système — 700/CNS)"
    BRAND = "Sony"
    CATEGORY = CAT_CCU
    AVAILABLE = True         # paint + activation VALIDÉS via capture MITM d'un vrai RCP (2026-08-06)
    DEFAULT_PORT = DEFAULT_PORT
    NEEDS_AUTH = False       # pas d'auth CNS (contrôle d'accès par assignation RCP, pas login)
    PRESET_RANGE = (0, 0)    # pas de mémoires sur ce transport
    NOTE = ("Paint + tally d'une caméra système Sony via sa CCU (protocole 700/CNS, TCP 7700). "
            "Protocole reconstitué par CAPTURE MITM d'un vrai RCP -> HSCU-1700 (2026-08-06) : "
            "handshake (ACK marqué 02 02 00), ACTIVATE (équivalent du bouton Activate), et paint "
            "en CHU_ANALOG 16 bits. « rcp_serial » = série de l'émulateur RCP.")

    def __init__(self, *a, **kw):
        # champ parc supplémentaire : numéro de CCU (repli d'adressage ; en BRIDGE le fil porte 0)
        self.ccu_number = int(kw.pop("ccu_number", 0) or 0)
        # série de l'ÉMULATEUR RCP présentée dans le handshake : une série nulle est REJETÉE
        # par la CCU (cf. DEFAULT_RCP_SERIAL). Surchargeable par le parc pour usurper un RCP réel.
        self.rcp_serial = int(kw.pop("rcp_serial", 0) or 0) or DEFAULT_RCP_SERIAL
        super().__init__(*a, **kw)
        self._engine = None
        self._tally = {"red": None, "green": None, "raw": None}  # None = pas encore lu
        # cache des valeurs POUSSÉES par la CCU : (group_pair, param0) -> valeur. La CCU envoie
        # tout l'état après l'ACTIVATE (groupes ABS 0x21/0x23/0x41/0x43), puis à chaque changement.
        self._state = {}

    @classmethod
    def models(cls):
        return [{"value": v, "label": l, "family": ""} for v, l in MODELS]

    # -- connexion ---------------------------------------------------------

    @staticmethod
    def _make_notify(state, tally):
        """Fabrique un callback lié à un état/tally donnés (portés PAR le moteur, donc partagés
        entre instances de pilote qui réutilisent la même session)."""
        def notify(header, payload):
            try:
                inner = parse_message50(payload)
            except DriverError:
                return
            for g, p0, val in parse_cmd_pairs(inner["cmd_buffer"]):
                fam = g & 0xFE            # 0x23->0x22, 0x41->0x40 (ABS -> REL, clé PAINT_ADDR['req'])
                if fam in (0x22, 0x42) and val > 0x7FFF:
                    val -= 0x10000        # analogiques = 16 bits SIGNÉS (capté : 0xff80 = -128)
                state[(fam, p0)] = val
            tally["raw"] = payload.hex()
        return notify

    def _connect(self):
        """Réutilise la session CNS persistante de cette CCU (ou l'établit : handshake + rituel
        tête caméra). En BRIDGE, `host` EST l'IP de la CCU."""
        key = (self.host, self.port)
        with _ENGINES_LOCK:
            eng = _ENGINES.get(key)
            if eng is not None and eng.connected:
                self._engine = eng
                self._state = eng.paint_state
                self._tally = eng.tally_state
                return eng
        # établir une nouvelle session hors verrou (I/O réseau)
        state, tally = {}, {"red": None, "green": None, "raw": None}
        eng = CnsEngine(self.host, self.port, mode=MODE_BRIDGE, timeout=self.timeout,
                        serial=self.rcp_serial, notify_cb=self._make_notify(state, tally))
        eng.connect()
        eng.paint_state = state
        eng.tally_state = tally
        with _ENGINES_LOCK:
            old = _ENGINES.get(key)
            if old is not None and old.connected:     # course : un autre thread a établi entre-temps
                try: eng.disconnect()
                except DriverError: pass
                self._engine = old; self._state = old.paint_state; self._tally = old.tally_state
                return old
            _ENGINES[key] = eng
        self._engine = eng
        self._state = state
        self._tally = tally
        self._ritual(eng)
        return eng

    def _ritual(self, eng):
        """Rituel de connexion du RCP — établit le LIEN TÊTE CAMÉRA (CHU), sans quoi la CCU
        accepte les octets mais n'APPLIQUE pas le paint. Reproduit la séquence captée d'un vrai
        RCP : ScanCameraStatus → interrogation du groupe CCU (0x40) → interrogation du groupe CHU
        (0x20, qui DÉCLENCHE la tête) → interrogation CHU analog (0x22 = valeurs paint). Les
        réponses poussées par la CCU alimentent le cache via _notify. (L'ACTIVATE `0b90…` n'est
        PAS émis : le vrai RCP ne l'envoie qu'au clic du bouton Activate, pas à la connexion.)"""
        def m10(blk):
            rid = eng.next_request_id()
            eng._raw_send(encode_packet(H_MESSAGE, struct.pack(">H", rid & 0xFFFF) + bytes([0x10]) + bytes(blk)))
        def query(group, p0s, width):
            pairs = b"".join((bytes([group, p, 0, 0]) if width == 4 else bytes([group, p, 0])) for p in p0s)
            rid = eng.next_request_id()
            eng._raw_send(build_message50(rid, 0, pairs, sub_type=M50_SUB_REQUEST))
        for x in (0x20, 0x40, 0xD3, 0xD4, 0x60):          # ScanCameraStatus
            m10([0x00, 0x40, 0x18, x, 0x00, 0x00])
        time.sleep(0.2)
        m10([0x03, 0x91, 0x18, 0x20, 0x00, 0x00]); time.sleep(0.2)
        query(0x40, RITUAL_G40, 3); time.sleep(0.2)       # état CCU
        query(0x20, RITUAL_G20, 3); time.sleep(0.25)      # DÉCLENCHE la tête caméra (CHU)
        # petite amorce de valeurs paint (les requêtes complètes se font à la demande dans
        # read_params ; on garde le lot du rituel COURT — un lot trop large fait fermer la CCU)
        self._query_paint([0x01, 0x02, 0x03, 0x60, 0xA9]); time.sleep(0.3)

    def _query_paint(self, p0s):
        """Interroge des valeurs paint (groupe CHU analog 0x22, paires 4 octets) par PETITS lots
        de param0 VALIDES ; les réponses (poussées par la CCU, sender CHU) alimentent le cache."""
        eng = self._engine
        if not eng:
            return
        for i in range(0, len(p0s), 10):           # lots de 10 max (évite un cmd trop long)
            chunk = p0s[i:i + 10]
            pairs = b"".join(bytes([0x22, p, 0, 0]) for p in chunk)
            rid = eng.next_request_id()
            try:
                eng._raw_send(build_message50(rid, 0, pairs, sub_type=M50_SUB_REQUEST))
            except DriverError:
                break
            time.sleep(0.08)

    def _msg_ccu_no(self):
        """ccu_no du Message50 = 0 en BRIDGE. CONFIRMÉ par capture d'un vrai RCP : il émet
        toujours ccu_no=0 sur le fil (le « n°8 » est le numéro système, pas l'octet réseau).
        Un ccu_no non nul (ex. 1) fait fermer la connexion."""
        return 0

    def _disconnect(self):
        # ferme ET retire la session partagée du cache (téardown explicite ; jamais appelé par
        # requête web, où les instances sont simplement jetées et la session reste vivante).
        eng = self._engine
        self._engine = None
        if not eng:
            return
        with _ENGINES_LOCK:
            if _ENGINES.get((self.host, self.port)) is eng:
                del _ENGINES[(self.host, self.port)]
        try:
            eng.disconnect()
        except DriverError:
            pass

    # -- identité & disponibilité -----------------------------------------

    def reachable(self):
        """Sondage léger : ouvrir + handshake. Un handshake OK prouve « CCU 700/CNS vivante »."""
        self.last_error = None
        try:
            eng = self._connect()
            return bool(eng.remote)
        except DriverError as e:
            self.last_error = str(e)
            return False

    def identify(self):
        eng = self._connect()
        info = eng.remote or {}
        model = info.get("model_name") or ""
        serial = info.get("serial")
        ident = {
            "model": model,
            "firmware": "",
            "serial": (str(serial) if serial else ""),
            "mac": "",
            "name": "",
        }
        if not model and not serial:
            raise DriverError("aucune identité dans le handshake (à confirmer en direct)")
        if model and not self.model:
            self.model = model
        return ident

    def capabilities(self):
        # CAP_POWER : le CAM PW (alimentation tête) existe (adresse 0x40/0x11) mais reste à
        # valider ; on l'annonce car l'écriture passe par le même chemin paint.
        return {CAP_PARAMS, CAP_TALLY, CAP_POWER}

    # -- paramètres (paint) -----------------------------------------------

    def params_schema(self):
        """Réglages colorimétriques (paint), adresses CONFIRMÉES par capture. Analogiques =
        16 bits signés. `role` positionne chaque réglage sur le pupitre RCP-3500 (diaph, noirs,
        blancs) ; `triplet`/`channel` regroupe les R/V/B côte à côte."""
        AMIN, AMAX, ASTEP, ABIG = -99, 99, 1, 10          # échelle pupitre Sony ±99 (cf. raw_to_disp)
        def a(key, label, grp, order, **kw):
            return param(key, label, "int", group=grp, color=True, validated=True,
                         min=AMIN, max=AMAX, step=ASTEP, big=ABIG, order=order, **kw)
        p = []
        # Diaphragme (fader du RCP)
        p.append(param("iris", "Iris (diaphragme)", "int", group="Exposition", color=True,
                       min=0, max=0x7FFF, step=64, big=512, validated=True, order=10, role="iris"))
        # Noir maître (molette pedestal) + triplet noir R/V/B
        p.append(a("black_master", "Noir maître", "Noir", 20, role="mblack"))
        p.append(a("black_r", "Noir rouge", "Noir", 21, role="black", triplet="black", channel="R"))
        p.append(a("black_g", "Noir vert",  "Noir", 22, role="black", triplet="black", channel="G"))
        p.append(a("black_b", "Noir bleu",  "Noir", 23, role="black", triplet="black", channel="B"))
        # Gains blancs R/V/B
        p.append(a("wb_gain_r", "Gain R", "Blancs", 30, role="white", triplet="wbgain", channel="R"))
        p.append(a("wb_gain_g", "Gain V", "Blancs", 31, role="white", triplet="wbgain", channel="G"))
        p.append(a("wb_gain_b", "Gain B", "Blancs", 32, role="white", triplet="wbgain", channel="B"))
        p.append(a("master_wgain", "Gain blanc maître", "Blancs", 33))
        # Gamma
        p.append(a("gamma",   "Gamma maître", "Gamma", 40))
        p.append(a("gamma_r", "Gamma R", "Gamma", 41, triplet="gamma", channel="R"))
        p.append(a("gamma_g", "Gamma V", "Gamma", 42, triplet="gamma", channel="G"))
        p.append(a("gamma_b", "Gamma B", "Gamma", 43, triplet="gamma", channel="B"))
        # Knee / Detail / clip / saturation
        p.append(a("knee_point", "Knee (point)", "Knee", 50))
        p.append(a("knee_slope", "Knee (pente)", "Knee", 51))
        p.append(a("white_clip", "White clip", "Knee", 52))
        p.append(a("detail",     "Detail",      "Detail", 60))
        p.append(a("saturation", "Saturation",  "Matrice", 70))
        # Matrice linéaire
        p.append(a("mtx_gr_r", "G→R", "Matrice", 71))
        p.append(a("mtx_br_r", "B→R", "Matrice", 72))
        p.append(a("mtx_rg_g", "R→G", "Matrice", 73))
        p.append(a("mtx_bg_g", "B→G", "Matrice", 74))
        p.append(a("mtx_rb_b", "R→B", "Matrice", 75))
        p.append(a("mtx_gb_b", "G→B", "Matrice", 76))
        # Interrupteurs CCU
        p.append(param("bars", "Barres de couleur", "bool", group="CCU", color=True,
                       validated=True, order=80))
        p.append(param("cam_power", "Alimentation tête (CAM PW)", "bool", group="CCU",
                       color=True, validated=True, order=81))
        return p

    def read_params(self, keys=None):
        """Valeurs paint courantes, lues dans le CACHE alimenté par les Message poussés par la
        tête caméra (sender CHU). On INTERROGE d'abord les param0 demandés (groupe 0x22), on
        laisse la CCU répondre, puis on lit le cache. Une clé non vue vaut None (jamais inventée).
        Analogiques sur 16 bits signés."""
        self._connect()
        want = set(keys) if keys else set(PAINT_ADDR.keys())
        # interroger les analogiques demandés (groupe CHU 0x22) pour rafraîchir le cache
        p0s = sorted({spec["req"][1] for k in want
                      for spec in [PAINT_ADDR.get(k)]
                      if spec and spec.get("req") and spec["req"][0] == 0x22})
        if p0s:
            self._query_paint(p0s)
            deadline = time.monotonic() + min(self.timeout, 1.2)
            while len(self._state) < len(p0s) and time.monotonic() < deadline:
                time.sleep(0.05)
        out = {}
        for key in want:
            spec = PAINT_ADDR.get(key)
            out[key] = None
            if not spec or not spec.get("req"):
                continue
            fam, p0 = spec["req"]                 # groupe « famille » (REL) + param0
            if (fam, p0) in self._state:
                raw = self._state[(fam, p0)]
                out[key] = raw_to_disp(raw) if _scaled(key, spec) else raw
        return out

    def write_param(self, key, value):
        """Écrit UN réglage paint. Interrupteur → 1 octet 0/1 ; analogique → valeur 16 bits sur
        le groupe ABS. CONFIRMÉ par capture : le RCP écrit p.ex. IRIS via `23 60 <hi> <lo>`."""
        spec = PAINT_ADDR.get(key)
        if not spec:
            raise Unsupported(f"réglage inconnu : {key}")
        if not spec.get("res"):
            raise Unsupported(f"adresse de « {key} » inconnue ({spec.get('note','')})")
        eng = self._connect()
        g, p0 = spec["res"]                       # groupe ABS (set absolu)
        if spec["kind"] == "switch":
            cmd = bytes([g, p0, 0x01 if value else 0x00])
            raw = 1 if value else 0
        else:
            # l'UI envoie l'échelle pupitre (±99) pour les réglages signés → reconvertir en 16 bits
            raw = disp_to_raw(int(value)) if _scaled(key, spec) else (int(value) & 0xFFFF)
            v = raw & 0xFFFF
            cmd = bytes([g, p0, (v >> 8) & 0xFF, v & 0xFF])   # valeur 16 bits big-endian
        rid = eng.next_request_id()
        pkt = build_message50(rid, self._msg_ccu_no(), cmd, sub_type=M50_SUB_REQUEST)
        # « TIRE ET OUBLIE » : on N'ATTEND PAS la réponse (comme un vrai RCP qui streame ses
        # variations). Attendre bloquerait chaque écriture → saccades en glissant diaph/pedestal.
        eng._raw_send(pkt)
        # refléter localement (valeur RAW comme le cache _notify) pour un retour immédiat
        self._state[(g & 0xFE, p0)] = raw
        return True

    # -- tally -------------------------------------------------------------

    def tally(self):
        """État tally connu : {red, green, raw}. `None` = pas encore observé.

        Le tally arrive en Notify (push) ; on renvoie le dernier état capté. Le décodage
        rouge/vert précis reste à établir en direct — d'où `raw` (payload brut hexa)."""
        self._connect()
        return dict(self._tally)

    # -- veille ------------------------------------------------------------

    def power(self, on):
        """CAM PW : alimentation de la tête caméra depuis la CCU (switch 0x40/0x11). NON VALIDÉ."""
        return self.write_param("cam_power", on)


# --------------------------------------------------------------------------- auto-test hors ligne

def _selftest():
    """Prouve le framing SANS matériel, contre les paquets réels des tests XUnit de Freehand
    (Tests/XUnitTest.PTP700/UnitTest1.cs). Lançable via `python3 drivers/sony_ccu.py`."""
    ok = 0
    fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  OK   {name}")
        else:
            fail += 1
            print(f"  FAIL {name}")

    # 1) ParseMultiPacket : 3 paquets dans un même segment
    data = bytes([0x0f, 0x02, 0x64, 0x6c, 0x0f, 0x02, 0x64, 0x6d,
                  0x0e, 0x0b, 0x79, 0x49, 0x02, 0x80, 0x00, 0x00,
                  0x01, 0x00, 0xc0, 0x80, 0xc0])
    pkts, consumed = iter_packets(data)
    check("multipaquet : 3 paquets", len(pkts) == 3)
    check("multipaquet : tout consommé", consumed == len(data))
    check("multipaquet : en-têtes 0f/0f/0e",
          [h for h, _ in pkts] == [0x0f, 0x0f, 0x0e])
    check("multipaquet : 3e = Message size 11", len(pkts[2][1]) == 0x0b)

    # 2) ParseMessage50_cmdGP_50_0 : décodage d'un vrai Message50 (réponse HSCU)
    m50 = bytes([0x0e, 0x18, 0x48, 0x9f, 0x50, 0x18, 0x01, 0x05, 0x00, 0x18,
                 0x40, 0x05, 0x00, 0x00, 0x0b, 0x50, 0x02, 0x08, 0x12, 0x00,
                 0xc2, 0x50, 0xc3, 0x00, 0xcb, 0x20])
    pkts, _ = iter_packets(m50)
    check("m50 : 1 paquet header 0x0e", len(pkts) == 1 and pkts[0][0] == 0x0e)
    info = parse_message50(pkts[0][1])
    check("m50 : id 0x489f", info["id"] == 0x489f)
    check("m50 : sub_type Response", info["sub_type"] == M50_SUB_RESPONSE)
    check("m50 : ccu_no 5", info["ccu_no"] == 5)
    check("m50 : sender HSCU", info["sender_srcid"] == SRC_HSCU)
    check("m50 : cmd_size 11", info["cmd_size"] == 11)
    check("m50 : cmd_buffer 11 octets", len(info["cmd_buffer"]) == 11)
    expected_cmd = bytes([0x50, 0x02, 0x08, 0x12, 0x00, 0xc2, 0x50, 0xc3, 0x00, 0xcb, 0x20])
    check("m50 : cmd_buffer exact", info["cmd_buffer"] == expected_cmd)

    # 3) round-trip build/parse Message50
    cmd = bytes([0x22, 0xAA, 0x2A])   # black R = valeur 0x2A
    built = build_message50(0x489e, 5, cmd, sub_type=M50_SUB_REQUEST, sender_srcid=SRC_RCP)
    pkts, _ = iter_packets(built)
    rt = parse_message50(pkts[0][1])
    check("round-trip : ccu_no", rt["ccu_no"] == 5)
    check("round-trip : cmd_buffer", rt["cmd_buffer"] == cmd)
    check("round-trip : valeur retrouvée", find_cmd(rt["cmd_buffer"], 0x22, 0xAA) == 0x2A)

    # 4) handshake : encode conforme à la CAPTURE d'un vrai RCP (model 0x0b, BRIDGE, id 0x6468)
    hs = build_handshake(H_HANDSHAKE, MODE_BRIDGE, 0x6468, serial=0)
    expected_hs = bytes([0x02, 0x12, 0x02, 0x01, 0x00, 0x01, 0x64, 0x68, 0x01,
                         0x90, 0x90, 0x0b, 0x00, 0x00, 0x00, 0x00, 0x64, 0x32,
                         0x0a, 0x05])
    check("handshake : octets exacts (unknown_1 = 02 01 00)", hs == expected_hs)
    # l'ACK doit porter unknown_1 = 02 02 00 (marqueur d'activation, sinon paint refusé)
    ack = build_handshake(H_HANDSHAKE_ACK, MODE_BRIDGE, 0x6468, serial=0)
    check("ACK : unknown_1 = 02 02 00", ack[2:5] == bytes([0x02, 0x02, 0x00]))
    check("ACK : reste identique au handshake", ack[5:] == hs[5:])
    # et parse round-trip
    pkts, _ = iter_packets(hs)
    hinfo = parse_handshake(pkts[0][1])
    check("handshake : mode BRIDGE", hinfo["mode"] == MODE_BRIDGE)
    check("handshake : id 0x6468", hinfo["id"] == 0x6468)

    # 5) découpage des paires de commande (switch 3 o / analog 4 o, 16 bits)
    pairs = parse_cmd_pairs(bytes([0x23, 0x60, 0x40, 0x6b, 0x41, 0x10, 0x01]))
    check("cmd_pairs : iris analog 16 bits", (0x23, 0x60, 0x406b) in pairs)
    check("cmd_pairs : switch 1 octet", (0x41, 0x10, 0x01) in pairs)

    print(f"\n{ok} OK, {fail} FAIL")
    return fail == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
