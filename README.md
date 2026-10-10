# Caméras — plugin Bobi.Tools

Pilotage d'un parc de caméras **multi-marques** (tourelles PTZ, caméras système via leur CCU)
et des pupitres Panasonic AW-RP, pour [Bobi.Tools](https://github.com/bob-integration/bobitools).
Chaque marque est un **pilote** isolé : l'interface n'affiche que ce que le pilote sait faire.

## Ce que fait l'outil

- **Parc** de caméras avec surveillance de disponibilité en tâche de fond, ajout en série,
  numérotation d'exploitation et profils de « presta » (export/import JSON, sans mots de passe).
- **Paramètres** lus et écrits caméra par caméra, **mémoires** nommées, onglet **RCP** de
  colorimétrie côte à côte, console de commandes brutes.
- **Actions groupées** sur plusieurs caméras à la fois (rappel de mémoire, format vidéo…),
  avec un compte-rendu caméra par caméra.
- **Sauvegardes** : instantané, comparaison avec l'état courant, restauration sélective.
- **Pupitres AW-RP** : table d'affectation caméra ↔ bouton, éditeur de macros, grille
  caméras × boutons, shotbox.
- **Ember+** (facultatif) : caméras et grille exposées à un contrôleur broadcast via le
  service [Ember+](https://github.com/bob-integration/bobitools-service-emberplus).

## À savoir

État des pilotes, tel que documenté dans le code et le journal :

| Pilote | Matériel | État |
|--------|----------|------|
| Panasonic AW (HTTP/CGI) | AW-HE130, AW-UE160 | opérationnel ; certains réglages restent en lecture seule, signalés comme tels |
| Sony CGI HTTP | ILME-FR7 | opérationnel ; SRG/BRC déclarés mais à confirmer |
| Sony CCU (700/CNS, TCP 7700) | HSCU-1700 | paint validé en direct |
| Sony VISCA over IP | SRG, BRC | écrit, jamais testé, non proposé |
| Générique | toute marque | simple fiche, aucune commande |
| Pupitre Panasonic AW-RP | AW-RP200 | affectations et macros validées ; RP150/120/60/50 non testés |

Changer la **fréquence** d'une caméra Panasonic la fait redémarrer (~2 min) : elle est
exclue des actions groupées. Le contenu des mémoires (cadrages) n'est pas sauvegardé, le
protocole ne permet pas de le relire.

## Prérequis

- **Bobi.Tools** avec **Docker** : l'outil tourne en conteneur (`runtime: docker`).
- Des caméras et pupitres joignables en réseau depuis l'hôte.
- Facultatif : le service [Ember+](https://github.com/bob-integration/bobitools-service-emberplus).

## Installation

Dans Bobi.Tools : **Réglages → Outils → Catalogue**, bouton « Installer ». Ou, sur une machine
neuve, en une ligne :

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh) --outils ptz
```

L'aide complète est dans [`help.md`](help.md), affichée dans Bobi.Tools (menu « ? » → Aide).
Les identifiants par défaut des caméras se règlent dans **Réglages → Outils**.

## Sécurité

- Le conteneur n'a pas d'authentification propre : son port n'est publié que sur
  `127.0.0.1`, il n'est joignable qu'à travers Bobi.Tools, qui contrôle les droits
  (permissions fines par caméra et par pupitre).
- Les mots de passe des caméras restent dans le volume de l'outil et **ne redescendent
  jamais au navigateur** ; c'est le serveur qui les pousse dans les pupitres.

## In English

Multi-brand camera fleet control for Bobi.Tools, built on isolated drivers: Panasonic AW
(HE130, UE160) and Sony CGI (ILME-FR7) are operational, the Sony CCU driver (700/CNS) is
validated for paint, Sony VISCA is written but untested. Availability monitoring, typed
parameters, named presets, side-by-side RCP, bulk actions with per-camera reports,
snapshots with selective restore, and Panasonic AW-RP panel assignment and macro editing.
Optional Ember+ exposure. Runs in Docker.

## Licence

GPL-3.0-or-later — © 2026 BOBI SAS. Voir [LICENSE](LICENSE).
