# Caméras tourelles (PTZ)

Pilotage d'un parc de caméras tourelles **multi-marques**. L'outil surveille leur
disponibilité, lit et écrit leurs paramètres, gère les mémoires (avec noms), et surtout
applique une action **sur plusieurs caméras à la fois**.

## Vérifier l'interface avant de livrer

`node mount_check.js .` monte l'UI contre un DOM minimal construit à partir des
identifiants réellement présents dans `page.html`, puis **rejoue tous les gestionnaires de
clic et de saisie**.

Ça ne dit rien de l'apparence, mais ça attrape la classe de panne qui passe à travers
`node --check` : une fonction supprimée par une réécriture de bloc, un identifiant absent du
HTML, un gestionnaire qui lève. À passer après toute modification de `page.js` ou
`page.html`.

## Architecture à pilotes

L'outil ne connaît aucun protocole : chaque marque est un **pilote** isolé dans
`drivers/`, qui expose le même contrat (`drivers/base.py`). Le serveur et l'interface ne
voient que ce contrat.

| Pilote | Marque | Transport | État |
|--------|--------|-----------|------|
| `panasonic_aw` | Panasonic (AW-UE…, AW-HE…) | HTTP / CGI `aw_ptz` + `aw_cam` | opérationnel |
| `sony_visca` | Sony (SRG, BRC) | UDP / VISCA over IP | **écrit, jamais testé** |

Deux mécanismes rendent l'interface indépendante de la marque :

- **les capacités** — chaque pilote annonce ce qu'il sait faire (mémoires, marche/veille,
  console…). L'UI n'affiche que ça. Une caméra qui ne sait pas faire quelque chose ne
  produit pas une erreur : la fonction est simplement absente.
- **le schéma de paramètres** — chaque pilote publie la liste de ses paramètres avec leur
  type et leurs valeurs possibles. L'UI construit les contrôles à partir de cette liste.

Ajouter une marque = déposer un module dans `drivers/` et l'importer dans
`drivers/__init__.py`. Ni le serveur ni l'interface ne changent.

## Ce qui est vérifié, et ce qui ne l'est pas

C'est le point le plus important de cette version.

Tout ce qui n'affiche pas de **⚠** a été confronté à une **AW-HE130 réelle** (firmware
V02.3800) : marche/veille, autofocus, vitesse de rappel des mémoires, gain, filtre ND,
obturateur, position zoom, **format et fréquence vidéo**.

| Réglage | Lecture | Écriture |
|---------|---------|----------|
| Format vidéo | `QSA:87` → `OSA:87:<hex>` | `OSA:87:<hex>` |
| Fréquence vidéo | `QSE:77` → `OSE:77:<n>` | `OSE:77:<n>` — **2 valeurs sur HE130** (59.94/50), **5 sur UE160** (+ 24, 23.98, 60 Hz) |
| Marche / veille | `#O` → `p0\|p1` | `#O0` / `#O1` |
| Autofocus | `#D1` → `d10\|d11` | `#D10` / `#D11` |
| Vitesse mémoires | `#UPVS` → `uPVS<000-999>` | `#UPVS<000-999>` |

Gain, filtre ND et obturateur sont **en lecture seule** : la requête est confirmée, mais ni
la commande d'écriture ni la correspondance code → valeur physique (dB, densité, 1/x s) ne
sont établies. Afficher `08` est honnête ; afficher `+9 dB` serait inventé.

### Les formats vidéo dépendent de la fréquence

L'outil ne propose que les formats de la fréquence courante, exactement comme l'interface
de la caméra. En 50 Hz : `1080/50p`, `1080/25p`, `1080/50i`, `1080/25PsF`, `720/50p`,
`576/50p(i)`, `576/50i`. En 59.94 Hz, la liste est différente.

Changer la **fréquence** fait **redémarrer la caméra** (~2 min) et réinitialise le format.
C'est pour ça qu'elle est volontairement exclue des actions groupées : c'est une bascule de
zone, pas un réglage d'exploitation. Le **format**, lui, est groupable — c'est le cas
d'usage principal de l'outil.

> Les codes de format sont **propres à la génération** : chaque famille a sa table, extraite
> de la caméra correspondante (16 codes sur HE130, 37 sur UE160 avec les formats 4K). Les
> codes communs aux deux concordent (`05`=1080/50i, `11`=1080/50p, `04`=1080/59.94i), ce qui
> les recoupe mutuellement.

### Comment lever une réserve : l'onglet Console

Deux méthodes, toutes deux utilisées pour cette version :

1. **« Sonder les commandes connues »** envoie une batterie de commandes de **lecture
   seule** (jamais d'écriture) et affiche les réponses brutes. La liste embarquée n'est pas
   devinée : elle vient de l'énumération exhaustive des 676 requêtes `Q`+2 lettres sur une
   HE130 réelle, dont 36 répondent.

2. **Lire les pages de réglages que la caméra sert elle-même.** C'est ce qui a débloqué le
   format vidéo : les commandes à paramètre (`OSA:87:xx`) sont invisibles à l'énumération,
   mais le JavaScript de la page `setup_camera_system.html` de la caméra les contient — avec
   la table de codes complète. Sur une autre génération, chercher `createFormatList` dans
   la page correspondante, puis confirmer par `QSA:87` dans la Console.

La console accepte aussi n'importe quelle commande à la main, sur le canal tourelle
(`aw_ptz`, préfixe `#`) ou caméra (`aw_cam`, requêtes `Q…`).

## Mémoires et renommage

Le nommage dépend du modèle, et l'outil s'adapte tout seul.

| | AW-UE160 | AW-HE130 |
|---|---|---|
| Nom stocké **dans la caméra** | oui (`QSJ:35` / `OSJ:35`) | non (`ER1`) |
| Repli sur le nommage local | inutile | systématique |

Sur une caméra qui sait nommer, l'outil **écrit le nom dans la machine** — il est donc
visible partout, y compris hors de Bobi.Tools. Sur les autres, le nom est tenu par l'outil ;
la fonction marche pareil à l'usage, seule la portée change. Le tableau des mémoires indique
laquelle des deux voies s'applique.

### Ce que la caméra accepte comme nom

Contraintes **mesurées** sur AW-UE160, aucune n'est documentée :

- **15 caractères** maximum (16 → `ER1`) ;
- **pas d'espace** — la CGI répond « 400 Bad Request », même pour un espace au milieu ;
- **pas de tiret**, même réponse ;
- les **accents** sont stockés sous forme encodée (`Régie` ressort `R%C3%A9gie`).

L'outil adapte donc la saisie : les accents sont translittérés (`Régie` → `Regie`) et tout
caractère hors jeu sûr devient `_` (`Plateau large` → `Plateau_large`). Le nom retenu est
**relu depuis la caméra** juste après l'écriture, donc ce que tu vois est ce qui est
réellement dans la machine — jamais une saisie transformée en silence.

Sur une caméra sans nommage embarqué, aucune de ces contraintes ne s'applique : le nom est
conservé tel quel, accents et espaces compris.

### Tableau de renommage

L'onglet **Noms des mémoires** présente une matrice : **mémoires en lignes, caméras en
colonnes**. Un parc a peu de caméras et beaucoup de mémoires — le défilement vertical est
donc le sens naturel, et une caméra reste lisible dans sa colonne du haut en bas.

L'en-tête (les caméras) et la colonne des numéros restent **figés** pendant le défilement :
sur cent lignes, on doit toujours savoir quelle mémoire et quelle caméra on remplit.

Chaque case est éditable ; on saisit puis on quitte la case. Une case **teintée** part
réellement dans la caméra, une case normale est tenue par l'outil — la portée n'est pas la
même et ça se voit sans survoler. Une étiquette sous le nom de chaque caméra rappelle
laquelle des deux voies s'applique à ce modèle.

Une plage (`de … à …`) et un filtre « n'afficher que les nommées » restreignent l'affichage.
Le filtre garde une mémoire dès qu'**au moins une** caméra y a un nom, pour qu'on voie
justement les cases à remplir.

Une caméra injoignable signale son état **une seule fois, dans son en-tête** — pas sur
chacune de ses cent cases — et sa colonne est grisée.

> **Toute lecture en masse sonde d'abord la caméra une seule fois.** C'est vrai des trois
> vues transverses — vue d'ensemble, matrice des noms, instantané. Sans ce garde, une
> machine éteinte imposait un délai d'attente à *chacune* des lectures suivantes : huit pour
> la vue d'ensemble, une centaine pour les noms. Un parc de six caméras dont quatre hors
> service s'affiche en trois secondes au lieu de plusieurs dizaines.

### Fonctions

- édition directe du nom dans la grille d'une caméra (1 à 100) ;
- filtre « n'afficher que les nommées » ;
- **renommage groupé** : la même mémoire renommée sur plusieurs caméras d'un coup. Le nom
  est poussé dans les caméras qui savent le stocker et tenu localement pour les autres ; le
  compte-rendu précise, ligne par ligne, laquelle des deux voies a servi.

Vider un nom sur une caméra qui les stocke **efface** l'entrée (`OSJ:36`), ce qui rétablit
le libellé d'usine (`Preset001`…).

## Les vues

L'outil se lit en **onglets de premier niveau**, exclusifs : chacun prend toute la page.

| Vue | À quoi elle sert |
|-----|------------------|
| **Parc** | ajouter et régler les caméras (le parc COMPLET, y compris les non utilisées) |
| **Vue d'ensemble** | où en est le parc *utilisé* : formats vidéo et de sortie, d'un coup d'œil |
| **Noms des mémoires** | matrice de renommage, mémoires en colonnes × caméras en lignes |
| **Sauvegardes** | instantanés, comparaison, restauration sélective |
| **Pupitres** | parc de pupitres AW-RP : affectation caméra ↔ bouton, et macros |
| **Grille** | placer les caméras utilisées sur les boutons des pupitres, en un écran |
| **Sélection presta** | choisir/numéroter les caméras d'une presta, profils, export/import |

### Parc fixe et caméras « utilisées »

Le **parc est fixe** : on y déclare toutes les caméras une fois. Pour une **presta**, on
n'« active » que celles qu'on veut, en leur donnant un **numéro** (vue *Sélection presta*).
Une caméra **sans numéro n'est pas utilisée** : elle disparaît des vues d'exploitation (Vue
d'ensemble, Noms, affectation, Grille) — mais reste dans le **Parc** et dans **Ember+** (où
l'on peut justement la numéroter). Un **profil de presta** est un jeu nommé « caméra →
numéro » qu'on enregistre et recharge ; le parc et les profils s'**exportent/importent**
(JSON, sans les mots de passe).

Les trois vues transverses sont **chargées à la demande** et gardées en mémoire : passer de
l'une à l'autre ne relit pas les caméras. Le bouton ↻ de chaque vue force la relecture, et
toute modification du parc (ajout, suppression) les invalide automatiquement.

### Affichage progressif

Une vue transverse ne fait plus attendre le parc entier. Le **squelette** — lignes, colonnes,
noms de caméras — se dessine immédiatement à partir de ce que le pilote déclare *sans toucher
aux caméras* : sorties disponibles et plage de mémoires sont des propriétés du modèle, pas
des valeurs à aller chercher.

Chaque caméra remplit ensuite sa ligne (ou sa colonne) **quand elle répond**, indépendamment
des autres. Une machine éteinte affiche son motif d'erreur à sa place, au bout de son délai,
sans retenir les caméras qui, elles, ont déjà répondu. Le compteur en haut de la vue indique
l'avancement (`3/6 lues…`).

### Suivi de connexion en continu

L'état de connexion est rafraîchi **toutes les 10 secondes**, quelle que soit la vue ouverte.
Ce rafraîchissement ne touche pas aux caméras : il lit l'état relevé par la surveillance de
fond du conteneur (réglage `poll_interval`, 10 s par défaut), dont le sondage est la commande
la plus légère du protocole.

- Une caméra qui **tombe** voit sa pastille passer au rouge et sa ligne se griser. Les
  dernières valeurs lues **restent affichées** — les effacer perdrait l'information sans
  rien apprendre ; l'infobulle précise le motif de la coupure.
- Une caméra qui **revient** est automatiquement relue et se remplit toute seule, sans
  qu'on ait à recharger quoi que ce soit.

## Vue d'ensemble du parc

L'onglet **Vue d'ensemble** montre un tableau transverse : une ligne par caméra, une colonne
par sortie vidéo, plus le modèle et la fréquence. C'est la réponse à « où en est tout le
parc ? » sans ouvrir les caméras une par une.

Les colonnes sont l'**union** des sorties du parc. Une caméra qui n'a pas une sortie laisse
la case grisée — à ne pas confondre avec « non lu », affiché en italique quand la sortie
existe mais n'a pas répondu. Une caméra injoignable occupe sa ligne avec son motif d'erreur,
elle n'est jamais affichée comme une caméra sans réglages.

Un clic sur le nom ouvre la caméra dans le détail.

## Ajouter plusieurs caméras d'un coup

Le bouton **+ Ajouter en série** crée une plage de caméras consécutives : on donne la
première adresse IP, le nombre, et des identifiants **communs à toute la série**.

Un aperçu affiche la plage exacte (`10.10.11.31 → 10.10.11.34`) et les noms générés **avant**
la création, pour qu'une faute de frappe se voie tout de suite. Deux garde-fous :

- la série ne franchit jamais `.254` — elle est refusée plutôt que de déborder sur un autre
  sous-réseau et de créer des entrées pointant vers d'autres machines ;
- une adresse **déjà au parc** est signalée, jamais dupliquée.

Chaque caméra est identifiée dans la foulée : le compte-rendu dit lesquelles ont répondu et
avec quel modèle. Celles qui ne répondent pas sont **créées quand même** — c'est souvent une
caméra pas encore branchée — mais apparaissent en échec dans le rapport.

## Tableau des sorties vidéo

Les caméras haut de gamme (AW-UE160…) ont un réglage de format **général** *puis* un réglage
**par sortie** (SDI, HDMI, IP). Les modèles plus simples n'ont que le général. L'onglet
Paramètres présente donc les formats sous forme de **tableau : une ligne par sortie**, la
première étant le réglage général.

Sur une AW-HE130, ce tableau n'a qu'une ligne — c'est le comportement attendu, pas un
manque. L'interface ne teste jamais le modèle : chaque pilote étiquette ses paramètres avec
la sortie à laquelle ils se rattachent (champ `output` du schéma) et déclare la liste de ses
sorties. Une génération qui en expose six produira six lignes sans qu'on touche à l'UI.

Sur une AW-UE160, le tableau a **sept lignes**, relevées et vérifiées en direct :

| Sortie | Lecture | Écriture |
|---|---|---|
| Réglage général | `QSA:87` | `OSA:87:<hex>` |
| 12G SDI | `QSJ:1E` | `OSJ:1E:<hex>` |
| 3G SDI OUT1 | `QSJ:21` | `OSJ:21:<hex>` |
| 3G SDI OUT2 | `QSJ:23` | `OSJ:23:<hex>` |
| HDMI | `QSJ:25` | `OSJ:25:<hex>` |
| Monitor | `QSL:AD` | — |
| Return | `QSL:B4` | `OSL:B4:<hex>` |

Toutes partagent la **même table de codes** que le réglage général — ce n'est pas une
supposition : la fonction `refresh12GSDISFPFormat` de la caméra compare les valeurs de
sortie aux mêmes codes (`"10"`, `"11"`, `"20"`) que le format système. Une sortie inactive
répond `FF`, affiché `—` : c'est une information, pas une lecture ratée.

> Les formats **par sortie** sont en **lecture seule**. La commande d'écriture est connue,
> mais la liste des formats admissibles sur une sortie dépend du format système *et* du mode
> de recadrage UHD. Proposer un choix sans cette logique produirait des refus
> incompréhensibles en exploitation.

> Sur la famille UE, la liste des formats **généraux** proposés est **déduite** de la cadence
> portée par le libellé — la fonction de construction de la caméra n'a pas été localisée. Le
> paramètre porte donc un ⚠ : la commande est sûre, la liste des choix est une inférence, et
> un format refusé par la caméra est signalé tel quel.

## Sauvegardes : instantané, comparaison, restauration

Le cycle est : **relever → comparer → renvoyer**.

Un **instantané** couvre une *sélection* de caméras — tout le parc par défaut, ou les
caméras cochées. Il relève **tout ce qui est lisible**, y compris ce que l'outil ne sait pas
réécrire : ça ne coûte rien et c'est ce qui rend la comparaison utile là où la restauration
ne l'est pas.

### Relevé ≠ restaurable

C'est la distinction à garder en tête :

| | Porte sur |
|---|---|
| **Comparaison** | tous les paramètres relevés |
| **Restauration** | uniquement ceux que le pilote sait écrire |

Sur une HE130 aujourd'hui : format vidéo, fréquence, autofocus, marche/veille et vitesse de
rappel sont restaurables ; gain, filtre ND, obturateur et position zoom ne le sont pas — leur
lecture est confirmée mais pas leur commande d'écriture. Le tableau de comparaison le dit
ligne par ligne : une ligne non restaurable affiche `—` au lieu d'une case à cocher, avec
l'explication au survol. **La détection de dérive reste donc complète même là où on ne sait
pas corriger.**

### Restauration sélective

Rien n'est réécrit sans être coché. Les écarts restaurables sont cochés par défaut — c'est
l'intention normale — mais l'envoi demande une validation explicite, et le compte-rendu est
ligne par ligne comme pour les actions groupées.

Deux garde-fous :

- **L'ordre d'écriture est imposé par le pilote.** Chez Panasonic la fréquence vidéo doit
  être écrite *avant* le format, sinon la caméra refuse un format qui n'appartient plus à la
  zone courante. Le schéma porte un champ `order` et la restauration trie dessus — le
  résultat ne dépend donc pas de l'ordre des cases cochées.
- **Les écritures lourdes sont signalées.** Un paramètre marqué `⏻` fait redémarrer la
  caméra (~2 min) ; s'il figure dans la sélection, la confirmation le dit explicitement.

### Ce qu'un instantané ne contient pas

**Le contenu des mémoires.** Le protocole AW permet de *rappeler* une mémoire, pas de *lire*
la position qu'elle contient. Un instantané enregistre donc les **noms** de mémoires (c'est
l'outil qui les tient) mais **pas les cadrages**. Sauvegarder les positions supposerait de
les relire, ce qu'aucune commande ne permet.

Une caméra injoignable au moment du relevé est **conservée dans l'instantané et marquée en
erreur**, jamais silencieusement omise : un instantané doit dire ce qu'il a raté.

## Actions groupées

Cochez les caméras dans la liste : la barre d'actions groupées apparaît.

- **rappeler une mémoire** sur toute la sélection ;
- **appliquer un paramètre** (ceux marqués « groupable » dans le schéma du pilote) ;
- **renommer une mémoire** sur toute la sélection ;
- **identifier** (relever modèle et firmware).

Le résultat est **toujours un compte-rendu ligne à ligne**, jamais un verdict global : une
action groupée réussit presque toujours *partiellement*, et une caméra restée en arrière
doit se voir. Le tableau donne pour chaque caméra son succès et, en cas d'échec, le message
exact renvoyé par la caméra.

> Le schéma de paramètres proposé dans la barre groupée est celui de la **première caméra
> sélectionnée**. Si la sélection mélange des modèles très différents, appliquez plutôt par
> groupe homogène.

## Surveillance de disponibilité

Une boucle de fond sonde le parc (intervalle réglable dans les réglages de l'outil, 20 s par
défaut) avec la commande la plus légère qui prouve à la fois que l'hôte répond *et* que
c'est bien une tourelle.

Trois états sont distingués et **jamais confondus** :

| Pastille | Sens |
|----------|------|
| verte | joignable (avec la latence mesurée) |
| rouge | sondée et **sans réponse** — le message d'erreur est affiché |
| grise | **pas encore sondée** — on ne prétend pas qu'elle est en panne |

## Réglages de l'outil

| Réglage | Rôle |
|---------|------|
| `default_user` / `default_password` | identifiants utilisés pour les caméras qui n'ont pas les leurs |
| `poll_interval` | intervalle de la surveillance, en secondes |

Les identifiants propres à une caméra priment sur les valeurs par défaut. Ils sont stockés
dans le volume de l'outil et **ne redescendent jamais au navigateur** : l'interface n'en
reçoit qu'un booléen « mot de passe renseigné », et un champ laissé vide à l'édition
signifie « ne change rien ».

## Volet pupitre (vue « Pupitres »)

Un pupitre Panasonic AW-RP tient une **table d'affectation** : quel numéro de caméra *au
pupitre* (C001…C200) pointe vers quelle caméra réelle (adresse, identifiants). Cette vue
lit et écrit cette table. C'est un **second type de pilote**, distinct de celui des caméras
(un pupitre n'a ni mémoires ni paramètres image) : sous-système `panels/`, jumeau de
`drivers/`.

### Comment le protocole a été établi

Aucune documentation publique. Même méthode que pour le format vidéo des caméras : **lire
le JavaScript que le pupitre sert lui-même** (`js/pc/setting_connect_assign.js`), confronté
à un **AW-RP200 réel**. L'interface est du Panasonic classique (canal `/cgi-bin/…`), mais
l'authentification est en **Digest MD5** (realm « Control »), pas en Basic comme les caméras.

| | Requête | Détail |
|---|---------|--------|
| Lecture | `GET /cgi-bin/get_cam_assign_edit` | 200 slots, 5 champs chacun : `control_type` (1=LAN, 2=Série, 3=NoAssign), `ipv4_addr`, `port`, `user`, `pass` |
| Écriture | `POST /cgi-bin/set_cam_assign_edit` | la table **entière** des 200 slots, mêmes clés ; succès = **204** |

> **Lecture ET écriture confirmées sur RP200 réel** (aller-retour réversible sur un slot
> libre). Piège d'écriture, appris à ses dépens : le CGI `set_cam_assign_edit` exige un
> en-tête **`Referer` vers `/admin/…`** et en fait un **match strict** — le port par défaut
> doit être **omis** (`http://host/…`, surtout pas `http://host:80/…`, sinon 400). C'est ce
> qui faisait échouer toute écriture (« 400 Bad Request »). La lecture, elle, passe sans.

### Ce que fait la vue

- **Parc de pupitres** à gauche (ajout / édition / suppression), avec pastille de
  disponibilité rafraîchie par une surveillance de fond — exactement comme les caméras.
- **Table d'affectation** à droite pour le pupitre choisi : une ligne par slot. Chaque slot
  se règle par une **liste déroulante des caméras du parc** — on affecte une caméra, pas une
  adresse à recopier. Le libellé « Non affecté » repasse le slot en NoAssign.
- Une IP déjà présente dans le pupitre mais **inconnue du parc** est conservée telle quelle
  (option « actuel : … ») : on ne l'écrase jamais par mégarde.

### Deux garde-fous

- **Le mot de passe caméra ne passe jamais par le navigateur.** Le front envoie un
  `camera_id` ; c'est le **serveur** qui résout adresse, port, identifiant et mot de passe
  depuis le parc et les pousse dans le pupitre. Le pupitre, lui, renvoie les mots de passe
  en clair à la lecture — raison de plus pour ne pas les faire transiter côté client.
- **Écrire est une modification d'exploitation**, donc **confirmée** et **minimale** : seuls
  les slots réellement changés partent ; le pilote relit la table brute et **réécrit le
  reste à l'identique** (mots de passe des autres slots compris), puis l'UI **relit** le
  pupitre pour afficher ce qui y est réellement, jamais la saisie supposée appliquée.

### Édition des macros

La sous-vue **Macros** (bascule en haut du panneau, à côté d'**Affectations**) lit et écrit
les **100 macros** du pupitre. Une macro est une suite d'étapes typées ; on édite une
**copie**, rien ne part au pupitre tant qu'on n'a pas cliqué **Enregistrer**.

| Requête | Rôle |
|---------|------|
| `GET /cgi-bin/get_macro_step?select=<n>` | étapes de la macro n : `cam_i`, `cmd_i`, `interval_i`, `format_i` |
| `POST /cgi-bin/macro_save` | écrit la macro entière (500 emplacements, mêmes clés) ; **Referer requis**, succès 204 |
| `POST /cgi-bin/macro_control` | `operate` 1=jouer / 0=arrêter, `select`=n° |

Types d'étape pris en charge (chacun traduit en commande native, et **relu** pour affichage) :
**Rappel mémoire** (`#R<n-1>`, avec la caméra visée), **Appeler une macro**
(`RECALL_MACRO:NNN`), **Attente utilisateur** (`WAIT_USER_TRIGGER`), **Commande AW brute**,
**CGI GET/POST**. Toute commande inconnue relue est présentée en « commande brute » et
**réécrite à l'identique** : un aller-retour ne dénature jamais une macro.

> **Règles de séquence du firmware**, apprises en direct : une macro **ne peut pas se
> terminer par une attente utilisateur** (l'outil le refuse avec un message clair, avant
> l'envoi) ; le pupitre rejette aussi certaines autres combinaisons (ex. attente suivie d'un
> appel de macro) — ressorties en « séquence refusée » plutôt qu'un `400` opaque. Le cas
> d'usage principal — **un tour de mémoires avec intervalles** — fonctionne sans réserve.

**Jouer / Arrêter** pilotent les **vraies caméras** (confirmation demandée avant de jouer).
Validé sur RP200 réel par un aller-retour réversible sur une macro libre.

### Ce qui reste

Les autres modèles de la gamme (RP150/120/60/50) partagent a priori le même protocole
(affectations et macros) mais ne sont pas confrontés au matériel. Les touches **USER** du
pupitre (`get_user_btn`/`set_user_btn`, affectation de fonctions aux boutons) sont repérées
mais pas exposées — accessoire par rapport aux affectations et aux macros.

## Contribution Ember+ (pupitre VSM)

L'outil s'annonce dans le **service Ember+ global** (`"ember": true`). Il expose deux volets.

**1. Pilotage des caméras** — un nœud par caméra dans le groupe **Caméras** :

| Paramètre | Sens |
|-----------|------|
| Numéro · Joignable · Modèle | informations, lecture seule |
| **Marche** | marche / veille, inscriptible |
| **Format vidéo** | liste des formats de la fréquence courante, inscriptible |
| **Mémoires → Rappeler** | rappelle une mémoire (déplace la caméra), inscriptible |

**2. La grille, en UNE MATRICE à points de croisement** (nœud **Grille caméras**) — forme
native VSM. Deux familles de sources et deux familles de cibles ; seuls deux croisements ont
un sens :

| Croisement | Effet |
|-----------|-------|
| **caméra** (source) × **cible numéro** | numérote la caméra (« Caméra 3 ») |
| **numéro** (source) × **cible bouton** | affecte ce numéro au bouton du pupitre |
| **Disconnect** (source 1) × n'importe quoi | efface (retire le numéro / vide le bouton) |

**Sources (lignes)** :

| Sources | Contenu |
|---------|---------|
| **1** | ⏻ Disconnect |
| **2 … 1+N** | les **caméras renseignées** (parc) — servent à numéroter |
| **à partir de l'offset** | les **numéros** 1 → `grid_cameras` — servent à remplir les boutons |

L'offset est `grid_cameras` **arrondi à la dizaine supérieure** (→ 101, 151, 201…), relevé si
le parc est gros, pour ne jamais chevaucher les sources « caméra ».

**Cibles (colonnes)**, numérotées à plat (défauts 100/10) :

| Cibles | Signification |
|--------|---------------|
| **1 … 100** | Numéros de caméra — cible *n* = « Caméra *n* » |
| **101 … 110** | Pupitre 1, boutons 1 → 10 |
| **111 … 120** | Pupitre 2, boutons 1 → 10 |
| … | une tranche de `grid_buttons` par pupitre |

Formule : `cible bouton b du pupitre p = grid_cameras + (p−1) × grid_buttons + b`.

**Vue globale** (lignes = sources, colonnes = cibles ; ● = point de croisement) :

```
                              CIBLES (colonnes)
                        │ Numéros 1..100   │ Pupitre 1 (101..110)
 SOURCES (lignes)       │  1   2   3  …    │ 101 102 103 …
 ───────────────────────┼──────────────────┼──────────────────────
   1  ⏻ Disconnect      │                  │
   ── caméras ──        │                  │
   2  HE130             │          ●       │                        HE130 → Caméra 3
   3  UE160             │              ●   │                        UE160 → Caméra 7
   ── numéros ──        │                  │
 103  N°3 · HE130       │                  │      ●                 bouton 2 pupitre 1 → Caméra 3
 107  N°7 · UE160       │                  │  ●                     bouton 1 pupitre 1 → Caméra 7
```

Lecture : les **caméras** (lignes 2, 3) se croisent avec les **colonnes numéro** pour
*numéroter* (HE130 → colonne 3 = « Caméra 3 »). Les **numéros** (lignes 103, 107 = offset
100 + n°) se croisent avec les **colonnes bouton** pour *affecter* (n°3 → colonne 102 =
bouton 2 du pupitre 1). Les numéros apparaissent donc **en colonnes** (cibles à numéroter) et
**en lignes** (sources à poser sur les boutons).

**Indirection et propagation** : un bouton porte un **numéro**, pas une caméra. Le tool
résout numéro → caméra → IP et l'écrit sur le pupitre. Si « Caméra 3 » devient une autre
caméra, **tous les boutons portant le n°3 sont ré-écrits automatiquement** vers la nouvelle.

Les écritures de la matrice arrivent par `ember/connect` ; celles des paramètres caméra par
`ember/set`.

> **Contrainte du service, respectée par construction** : le service balaie `ember/tree`
> toutes les 5 s et **saute tout contributeur qui met plus de 10 s**. L'arbre se construit
> donc **sans jamais interroger les caméras** (~4 ms) : un **rafraîchisseur de fond** lent
> (réglage `EMBER_POLL`, 30 s par défaut) relève marche/veille et format des caméras
> **joignables** et les met en cache. Les écritures, elles, partent **en direct** et
> rafraîchissent aussitôt la caméra concernée. Une caméra injoignable reste dans l'arbre,
> marquée non joignable, sans ses contrôles pilotables.

## Grille (numéros + boutons des pupitres)

L'onglet **Grille** réunit sur un seul écran les deux opérations, dans l'ordre naturel :

1. **Numéroter les caméras** — colonne **N°** (éditable). On donne à chaque caméra son
   numéro d'exploitation : *HE130 → Caméra 3*. Le numéro réordonne l'affichage et sert de
   repère partout (parc, vue d'ensemble, noms, Ember+). Il est **unique** (un doublon est
   refusé) et **fixe** — indépendant des pupitres.

2. **Affecter aux boutons des pupitres** — lignes = caméras ; chaque pupitre est un **bloc
   de 10 colonnes** (les boutons 1→10), jusqu'à 10 pupitres. Cocher une case dit *« cette
   caméra sur ce bouton de ce pupitre »* : *Caméra 3 sur le bouton 2 du pupitre 4*.

Les deux axes sont **séparés** : une même caméra peut occuper un bouton différent d'un
pupitre à l'autre (plusieurs cadreurs se répartissent les caméras). Contraintes tenues par
la grille : un bouton ne pointe que vers **une** caméra, et une caméra n'occupe **qu'un**
bouton par pupitre (recliquer une case la libère).

Rien n'est envoyé avant **Enregistrer** : les numéros partent vers les caméras, les boutons
vers les pupitres (les positions au-delà de celles exposées restent **intactes**), puis la
grille est relue. C'est une écriture d'exploitation → confirmation demandée.

### Dimensions réglables

Les trois dimensions de la grille se règlent dans **Réglages de l'outil**, pour faire évoluer
l'échelle sans retoucher le code :

| Réglage | Rôle | Défaut |
|---------|------|--------|
| `grid_cameras` | caméras exposées (lignes de la grille, et numéro maximum) | 100 |
| `grid_panels` | pupitres exposés (blocs de colonnes) | 10 |
| `grid_buttons` | boutons exposés par pupitre (colonnes par bloc) | 10 |

Augmenter un chiffre agrandit la grille au prochain chargement. Les boutons **non exposés**
d'un pupitre (au-delà de `grid_buttons`) ne sont jamais touchés par la grille.
