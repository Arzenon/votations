# Pistes, gardées pour plus tard

Ce qui a été étudié sérieusement, chiffré, puis remis à plus tard. C'est un
carnet, pour que le travail de reconnaissance ne soit pas à refaire : chaque
entrée dit où sont les données, ce qui marche, et ce qui coince.

Une entrée peut être partiellement livrée, et le dit alors en tête de la section
concernée - c'est le cas des scrutins, faits pour les lois promulguées et pas
pour les textes rejetés. Ce qui reste écrit ici est ce qu'il faut savoir avant
d'aller plus loin, y compris les pièges découverts *en* implémentant, qui ne sont
pas ceux qu'on avait prévus.

---

## Importer aussi les textes **rejetés**

**Pourquoi.** L'import ne prend que les lois promulguées, ce qui penche
systématiquement du côté de ce que la majorité du moment a réussi à faire
adopter. Sur une plateforme dont la charte promet la symétrie entre les
arguments, c'est un biais de sélection en amont de toute présentation.

**État : non commencé.** Décidé le 26/08/2026 : on livre avec les seules lois
promulguées. Ce qui suit est la reconnaissance déjà faite.

### Il n'y a pas de nouvelle source à trouver

Les textes rejetés sont **déjà dans l'archive téléchargée tous les jours** par
`assemblee-refresh` (`AN_DOSSIERS_URL`, ~9,8 Mo) :

<https://data.assemblee-nationale.fr/static/openData/repository/17/loi/dossiers_legislatifs/Dossiers_Legislatifs.json.zip>

`app/assemblee.py` les écarte en une ligne - `PROMULGATION_ACTE = "PROM-PUB"`,
utilisé par `_find_promulgation`. L'issue d'un dossier ne se lit pas dans
`codeActe` mais dans **`statutConclusion.fam_code`**, porté par les actes de
décision (ceux dont le `codeActe` finit par `-DEC`).

| `fam_code` | libellé | où |
|---|---|---|
| `TSORTF07` | rejeté / rejetée | `AN1-DEBATS-DEC`, `SN1-DEBATS-DEC`, `SNNLEC-…`, `SN2-…` |
| `TSORTF24` | considéré comme rejeté (49.3) | `CMP-DEBATS-AN-DEC` |
| `TMRC01` | rejet du texte par la commission | `ANLUNI-COM-CAE-DEC` |
| `TCCMP02` | désaccord (échec de CMP) | `CMP-DEC` |
| `TCD04` | non conforme (Conseil constitutionnel) | `CC-CONCLUSION` |

### Le tri des 3 047 dossiers (17ᵉ législature, relevé le 26/08/2026)

| Issue | Dossiers |
|---|---|
| promulguée - *ce qu'on importe* | 147 |
| **rejetée en séance** | **20** |
| retirée par son auteur (`AN1-RTRINI`) | 82 |
| débattue, sans conclusion (en cours / caduque) | 229 |
| jamais débattue en séance | 2 569 |

> **La bonne frontière n'est pas « non promulguée ».** Ce serait noyer 20
> décisions démocratiques réelles sous 2 569 propositions déposées et jamais
> inscrites à l'ordre du jour. La frontière utile est « **est allée au vote en
> séance** », ce que `TSORTF07` / `TSORTF24` marque exactement.

Les 20 sont substantiels, pas de la procédure : nationalisation d'ArcelorMittal,
impôt plancher de 2 % sur le patrimoine, projet de loi constitutionnelle sur la
Nouvelle-Calédonie, réforme de l'audiovisuel public, annulation des dernières
réformes des retraites…

### Les scrutins : **faits**, pour les lois promulguées

> **Livré le 01/09/2026** - `app/scrutins.py`, colonnes `an_vote_*` et
> `cmp_convened` sur `laws` et `imported_laws`, affichés dans l'encart de la page
> de loi. Ce qui suit n'est plus une piste : c'est ce qu'il faut savoir si on
> étend la même mécanique aux textes rejetés, et **la moitié du travail est déjà
> écrite**.

`voteRefs.voteRef` sur l'acte de décision fait le lien avec un second export
(25 Mo, 8 434 scrutins) :

<https://data.assemblee-nationale.fr/static/openData/repository/17/loi/scrutins/Scrutins.json.zip>

Chaque scrutin porte `syntheseVote.decompte` (`pour`, `contre`, `abstentions`),
`syntheseVote.annonce`, `sort.code` et `objet.libelle`.

**Les membres de l'archive sont nommés `json/{uid}.json`.** On lit donc les ~158
scrutins référencés au lieu de décoder les 8 434 : le téléchargement reste ce
qu'il est, le décodage devient négligeable. `fetch_votes` prend l'ensemble des
références voulues et ne va chercher que celles-là.

#### Couverture mesurée sur les 147 lois promulguées (17ᵉ législature, 01/09/2026)

| | |
|---|---|
| décompte retenu | **94 (63 %)** |
| dont adoptées sans une voix contre | 37 |
| passées par une commission mixte paritaire | 56 (38 %) |
| sans scrutin public (adoptées à main levée) | 53 |

La CMP ne coûte rien : elle est dans le fichier des dossiers déjà téléchargé
chaque jour (`codeActe == "CMP"`).

#### Le piège de polarité : **il ne concerne pas les promulguées**

L'avertissement qui figurait ici avait été relevé sur les textes **rejetés**, où
six scrutins rattachés sur neuf sont l'adoption d'une motion de rejet - « pour
190 » y voulant dire cent quatre-vingt-dix voix *pour rejeter*. Vérification faite
sur le corpus promulgué : **zéro occurrence**. Tous les objets y sont
« l'ensemble de… » ou « l'article unique de… ».

Il reste vrai pour les rejetés, et c'est la première chose à traiter si on les
importe. Le garde-fou en place est le **code d'acte** : `app/assemblee.py` ne
collecte que les actes finissant par `-DEC` et ignore les `…-MOTION-VOTE`, dont
il y a onze sur cette législature. `app/scrutins.final_vote` n'accepte ensuite
que `sort == "adopté"`, ce qui est un second filet - et, pour les rejetés, ce
sera précisément le filtre à **inverser** : là-bas, l'information est dans
l'adoption d'une motion.

#### En revanche, deux pièges bien réels sur les promulguées

Trouvés en implémentant, pas prévus par la note d'origine :

1. **Le dernier `AN1-DEBATS-DEC` n'est pas toujours le vote final.** Trois lois de
   finances portent un scrutin `sort: rejeté` : l'Assemblée avait rejeté la
   première partie en première lecture, le texte étant adopté ensuite en CMP. Il
   faut parcourir **toutes** les lectures (`AN1`, `AN2`, `ANNLEC`, `ANLDEF`,
   `CMP-DEBATS-AN`) et retenir la dernière adoption. Ce n'est pas marginal : en
   ne lisant que la première lecture on trouve 73 décomptes, en les parcourant
   toutes on en trouve 94.
2. **« l'ensemble » ne suffit pas à reconnaître un vote sur le texte entier.** Un
   des trois se libelle « **l'ensemble de** la première partie du projet de loi de
   finances ». Un filtre sur « l'ensemble » le laisse passer ; le filtre en place
   écarte tout objet contenant « partie ».

#### Ce que l'export ne contient pas

**Les scrutins du Sénat.** Il publie les siens ailleurs. Tout ce que le site
affiche nomme donc explicitement « à l'Assemblée nationale », et l'étendre au
Sénat demanderait une troisième source.

### Le vrai obstacle : le texte lui-même

Un texte rejeté ne paraît jamais au Journal Officiel : **ni NOR, ni date JO** -
c'est-à-dire précisément la clé de `fetch_law_text`. Toute la chaîne de brouillon
IA est donc hors d'atteinte pour ces textes. Ce qui a été vérifié :

| Piste | Résultat |
|---|---|
| arbre `json/document/` de l'archive (7 061 documents) | métadonnées seules, `divisions: null` |
| `dyn/opendata/{uid}.json` *(déjà utilisé par `debates.py`)* | métadonnées seules |
| `dyn/opendata/{uid}.xml` | métadonnées seules |
| page publique du texte | coquille JavaScript, aucun texte dans le HTML servi |
| **PDF** - `…/dyn/17/textes/l17b0153_proposition-loi.pdf` | **200, 40 ko - ça marche** |

Le corps du texte n'est donc accessible qu'en PDF : nouvelle dépendance
(pypdf/pdfminer), et un motif d'URL dont le suffixe dépend du type de document
(`_proposition-loi`, `_projet-loi`, …) - un seul type a été vérifié.

Le lien dossier → documents se fait par `document.dossierRef` (les 20 dossiers
rejetés ont tous leurs documents dans l'archive).

### Ce que serait une phase 1 raisonnable

Importer les textes rejetés avec les **seules métadonnées déjà disponibles** -
titre, chambre, date, auteur, issue, lien vers le dossier, et le décompte quand
un scrutin existe - et rédiger les deux blocs de présentation à la main. Une
vingtaine de textes par législature, soit environ un par mois : c'est tenable.

Bonne nouvelle pour la moitié argumentative : **elle marche déjà**. Les arguments
viennent des comptes rendus que `app/debates.py` sait lire, et la séance où un
texte est rejeté est exactement celle où les arguments contre sont prononcés.

Deuxième bonne nouvelle depuis : **les décomptes aussi**. `app/scrutins.py`
télécharge l'export, lit les scrutins par identifiant et les rattache ; les
colonnes `an_vote_*` existent sur les deux tables et la page sait déjà afficher
« adoptée par N voix contre M » comme « sans scrutin public ». Il reste, pour les
rejetés, à écrire une variante de `final_vote` : là-bas l'information utile est
l'**adoption d'une motion de rejet**, donc le filtre `sort == "adopté"` doit être
inversé et le libellé lu pour connaître le sens (voir le piège de polarité plus
haut). Le vocabulaire d'affichage aussi : « adoptée par… » ne convient pas.

L'extraction PDF ne viendrait qu'en phase 2, si le volume le justifie.

### Les deux questions à trancher avant de coder

1. **Ça change la question posée.** La charte suit le modèle suisse : les
   citoyens réagissent à ce que le Parlement **a adopté**. Un texte rejeté
   demande « aurait-il fallu l'adopter ? » - plus proche d'une initiative
   populaire que d'un référendum. Les deux sont légitimes (la Suisse a les deux),
   mais la page de vote devra dire laquelle elle pose, et le vocabulaire d'état
   (`open` / `closed`, `app/models/law.py`) n'a pas aujourd'hui de place pour
   « ce texte a été rejeté ».
2. **Le champ `official_ref` et `legifrance_url` n'ont pas de sens ici** : pas de
   « LOI n° 2026-532 », pas de lien Légifrance. Il faudra soit les laisser vides,
   soit leur trouver un équivalent (numéro de dépôt, lien vers le dossier AN).

---

## Nommer qui a déposé le texte

**Pourquoi.** Un projet de loi vient du Gouvernement, une proposition de loi de
parlementaires. La réaction d'un citoyen n'est pas la même dans les deux cas, et
la page ne le dit nulle part. `procedureParlementaire.libelle` s'en approche
(« Projet de loi ordinaire », « Proposition de loi ») et est déjà importé, mais
il ne nomme personne.

**État : non commencé.** Reconnaissance faite le 01/09/2026.

Le dossier porte `initiateur` sur **145 des 147** lois promulguées, mais sous
forme d'identifiants :

```json
{"acteurs": {"acteur": [{"acteurRef": "PA643210", "mandatRef": "PM873637"}]}}
```

Les résoudre en noms demande un troisième export, celui des acteurs et mandats :

<https://data.assemblee-nationale.fr/static/openData/repository/17/amo/tous_acteurs_mandats_organes_xi_legislature/AMO30_tous_acteurs_tous_mandats_tous_organes_historique.json.zip>

**12 Mo**, ce qui est du même ordre que les dossiers eux-mêmes - raisonnable. Le
job d'import ferait alors trois téléchargements par nuit (10 + 25 + 12 Mo).

Deux précautions avant de coder :

* c'est une **donnée personnelle** au sens du RGPD, même publique et même
  publiée par l'Assemblée. Elle ne concerne pas les votants, donc rien de ce que
  la charte protège n'est en jeu, mais `/vie-privee` décrit aujourd'hui un site
  qui n'enregistre aucune personne : la page devra dire qu'il stocke les noms des
  parlementaires ayant déposé un texte, comme il stocke déjà ceux qui sont cités
  dans les arguments ;
* le nom seul est peu utile ; le groupe politique l'est davantage, et il se lit
  dans le même export via les mandats. C'est aussi ce qui invite à en faire une
  lecture partisane, ce que la charte refuse. À trancher avant, pas après.

---

## Paliers homogènes des répartitions par profil

**Pourquoi.** Un palier ne publie une catégorie que si chacune de ses cases a
bougé de 0 ou d'au moins `MIN_CELL_COUNT` votes (`app.crud.demographics._held`).
Mais une catégorie qui a gagné 10 votes, tous « Pour », passe ce filtre. Qui sait
que X a voté « Pour » pendant cette fenêtre (le décompte total est en direct, ou
X l'a dit) apprend alors sa catégorie, s'il en a donné une.

**État : non traité, limite connue.** Décidé le 18/10/2026. Le choix lui-même est
déjà donné par le décompte en direct : ce qui fuit, c'est le profil.

### Ce qui coince

Exiger au moins deux choix ayant bougé par catégorie (une forme de l-diversité)
fermerait le cas, mais retiendrait la plupart des catégories à chaque palier :
les votes d'une heure vont très souvent tous du même côté dans une petite
catégorie. Les répartitions resteraient figées des jours, ce qu'on vient
justement de corriger en passant d'une retenue par dimension à une retenue par
catégorie.

### Un cas dégénéré entre dimensions

Une catégorie retenue peut se déduire d'une autre dimension si chaque profil
renseigne tous les champs : le total des tranches d'âge, moins les régions
publiées, donne alors la région retenue. Il faudrait que la page prouve ce
remplissage complet (profils renseignés égaux aux votes, et total des âges égal
aux profils), ce qui devient improbable dès 50 profils par palier. Noté le
18/10/2026, sans correction.

---

## Écarté délibérément

Recensé le 01/09/2026 en inventoriant le portail, pour ne pas le réexaminer.

* **Amendements**
  (`.../loi/amendements_div_legis/Amendements.json.zip`) - **282 Mo**. Le nombre
  d'amendements déposés serait le meilleur indicateur de contestation
  disponible, bien meilleur que la CMP. Mais ce téléchargement dans un conteneur
  limité à 1 Go est hors de proportion avec le gain, et `app.opendata.fetch`
  garde tout en mémoire. À rouvrir seulement si l'export gagne un découpage par
  dossier.
* **Questions écrites** (44 Mo) - sans rapport avec ce que fait la plateforme.
* **`SyceronBrut.xml.zip`** - 404 sur cette législature. Les comptes rendus
  passent par `dyn/opendata/{uid}.json`, séance par séance, ce que
  `app/debates.py` fait déjà et qui évite tout téléchargement de corpus.
