# Pistes, gardées pour plus tard

Ce qui a été étudié sérieusement, chiffré, puis remis à plus tard. Rien ici n'est
en cours : c'est un carnet, pour que le travail de recherche ne soit pas à
refaire. Chaque entrée dit où sont les données, ce qui marche, et ce qui coince.

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

`app/assemblee.py` les écarte en une ligne — `PROMULGATION_ACTE = "PROM-PUB"`,
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
| promulguée — *ce qu'on importe* | 147 |
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

### Les scrutins : disponibles, et **piégeux**

`voteRefs.voteRef` sur l'acte de décision fait le lien avec un second export
(~26 Mo, 8 434 scrutins) :

<https://data.assemblee-nationale.fr/static/openData/repository/17/loi/scrutins/Scrutins.json.zip>

Chaque scrutin porte `syntheseVote.decompte` (`pour`, `contre`, `abstentions`),
`syntheseVote.annonce` et `objet.libelle`.

> ⚠️ **Le piège, à ne surtout pas manquer.** Deux façons de rejeter un texte, de
> polarité **opposée** :
>
> * « l'Assemblée n'a pas adopté **l'ensemble de** la proposition » → `pour 149 /
>   contre 174` se lit naturellement ;
> * « l'Assemblée **a adopté la motion de rejet préalable** » → `pour 190` veut
>   dire que **190 ont voté pour rejeter**.
>
> Six des neuf scrutins rattachés sont du second type. Afficher « pour 190 » à
> côté d'un texte rejeté dirait l'exact contraire de ce qui s'est passé. Il faut
> lire `objet.libelle` pour connaître le sens.
>
> Et 11 des 20 n'ont **aucun** `voteRef` : rejetés à main levée, il n'existe pas
> de décompte. La page devra le dire plutôt que d'afficher un vide.

### Le vrai obstacle : le texte lui-même

Un texte rejeté ne paraît jamais au Journal Officiel : **ni NOR, ni date JO** —
c'est-à-dire précisément la clé de `fetch_law_text`. Toute la chaîne de brouillon
IA est donc hors d'atteinte pour ces textes. Ce qui a été vérifié :

| Piste | Résultat |
|---|---|
| arbre `json/document/` de l'archive (7 061 documents) | métadonnées seules, `divisions: null` |
| `dyn/opendata/{uid}.json` *(déjà utilisé par `debates.py`)* | métadonnées seules |
| `dyn/opendata/{uid}.xml` | métadonnées seules |
| page publique du texte | coquille JavaScript, aucun texte dans le HTML servi |
| **PDF** — `…/dyn/17/textes/l17b0153_proposition-loi.pdf` | **200, 40 ko — ça marche** |

Le corps du texte n'est donc accessible qu'en PDF : nouvelle dépendance
(pypdf/pdfminer), et un motif d'URL dont le suffixe dépend du type de document
(`_proposition-loi`, `_projet-loi`, …) — un seul type a été vérifié.

Le lien dossier → documents se fait par `document.dossierRef` (les 20 dossiers
rejetés ont tous leurs documents dans l'archive).

### Ce que serait une phase 1 raisonnable

Importer les textes rejetés avec les **seules métadonnées déjà disponibles** —
titre, chambre, date, auteur, issue, lien vers le dossier, et le décompte quand
un scrutin existe — et rédiger les deux blocs de présentation à la main. Une
vingtaine de textes par législature, soit environ un par mois : c'est tenable.

Bonne nouvelle pour la moitié argumentative : **elle marche déjà**. Les arguments
viennent des comptes rendus que `app/debates.py` sait lire, et la séance où un
texte est rejeté est exactement celle où les arguments contre sont prononcés.

L'extraction PDF ne viendrait qu'en phase 2, si le volume le justifie.

### Les deux questions à trancher avant de coder

1. **Ça change la question posée.** La charte suit le modèle suisse : les
   citoyens réagissent à ce que le Parlement **a adopté**. Un texte rejeté
   demande « aurait-il fallu l'adopter ? » — plus proche d'une initiative
   populaire que d'un référendum. Les deux sont légitimes (la Suisse a les deux),
   mais la page de vote devra dire laquelle elle pose, et le vocabulaire d'état
   (`open` / `closed`, `app/models/law.py`) n'a pas aujourd'hui de place pour
   « ce texte a été rejeté ».
2. **Le champ `official_ref` et `legifrance_url` n'ont pas de sens ici** : pas de
   « LOI n° 2026-532 », pas de lien Légifrance. Il faudra soit les laisser vides,
   soit leur trouver un équivalent (numéro de dépôt, lien vers le dossier AN).
