# Votations Françaises

Plateforme citoyenne d'expression sur les lois françaises. Voir `CLAUDE.md` pour
la vision et les principes (transparence radicale, neutralité, aucune donnée
personnelle).

## Stack

- **Backend** : FastAPI (Python 3.12+)
- **Frontend** : templates Jinja2 rendus côté serveur + HTMX (progressif — tout
  fonctionne sans JavaScript)
- **Base de données** : PostgreSQL (SQLAlchemy 2 + Alembic).

## Mise en production

`docker-compose.yml` est le fichier de **développement**. Pour un déploiement réel,
voir **[DEPLOY.md](DEPLOY.md)** : secrets, choix d'hébergement, TLS, sauvegardes
quotidiennes et hygiène des journaux. L'application refuse de démarrer en
production tant que `SECRET_KEY` ou `ADMIN_PASSWORD` gardent leur valeur
d'exemple.

## Démarrage (Docker — recommandé)

Tout tourne dans Docker : PostgreSQL + l'application. Au démarrage, le conteneur
applique les migrations et insère des lois d'exemple (`SEED_ON_START=1`).

```bash
docker compose up --build
```

- Accueil : http://127.0.0.1:8000/
- Administration : http://127.0.0.1:8000/admin (HTTP Basic — `admin` / `change-me`,
  voir `docker-compose.yml`)

Tests et qualité (dans le conteneur) :

```bash
docker compose run --rm app pytest
docker compose run --rm app ruff check .
docker compose run --rm app ruff format --check .
```

L'outillage de test vit dans l'étage `dev` de l'image ; l'image de production n'en
contient rien, tourne sous un utilisateur non privilégié et installe exactement
les versions figées dans `requirements.lock`.

La suite construit son schéma **par les migrations**, pas par `create_all` : une
migration manquante casse donc les tests plutôt que la mise à jour en production,
et `tests/test_migrations.py` vérifie en plus que le schéma migré correspond aux
modèles et que la chaîne fait l'aller-retour.

## Démarrage sans Docker (optionnel)

Nécessite un PostgreSQL accessible (`DATABASE_URL`). Vous pouvez lancer
uniquement la base via `docker compose up -d db`.

```bash
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # ajuster SECRET_KEY, identifiants admin, DB
alembic upgrade head
python -m scripts.seed        # quelques lois d'exemple (idempotent)
uvicorn app.main:app --reload
```

## Architecture

```
app/
  main.py            Fabrique FastAPI, pages d'erreur, robots.txt, sonde /sante
  config.py          Réglages (DATABASE_URL, SECRET_KEY, identifiants admin…)
  db.py              Moteur SQLAlchemy (paresseux), get_db, utcnow partagé
  labels.py          Libellés français des états stockés en machine
  security.py        Refus des requêtes inter-sites, assainissement des URL
  ratelimit.py       Compteurs en mémoire (vote, connexions admin)
  profile_cookie.py  Profil démographique facultatif, gardé côté navigateur
  logging_config.py  Journalisation sans adresse IP
  drafting.py        Le brouillon IA, exécuté en tâche de fond
  models/            Law, Vote, ImportedLaw (aucune donnée personnelle stockée)
  crud/              laws, votes, results (agrégats simples + export anonyme), imports
  auth/              Couche d'authentification enfichable (voir ci-dessous)
  routes/            public, admin, auth
  assemblee.py       Import des lois promulguées (données ouvertes AN)
  legifrance.py      Texte officiel d'une loi (données ouvertes JO/DILA)
  ai.py              Brouillon IA : blocs factuels + sélection d'arguments
  debates.py         Interventions attribuées en séance (données ouvertes AN)
  templates/         base, home, law, results, search, connexion, admin/*
  static/            CSS minimal, htmx, app.js (rehaussement progressif)
alembic/             Migrations
tests/               pytest
```

### Le « joint » FranceConnect (préparé pour la V2)

Tout le code de vote ne dépend que de l'interface `AuthProvider`
(`app/auth/base.py`) :

- `get_voter_token(request, response)` → jeton **anonyme**, non identifiant,
  utilisé uniquement pour « un vote par loi ». Il en *crée* un si nécessaire :
  appelé au moment du vote, jamais à la lecture.
- `peek_voter_token(request)` → le jeton que le navigateur porte déjà, ou `None`,
  sans rien créer. C'est ce que lit la page d'une loi.
- `login_url()` / `logout_url()` / `is_authenticated()` / `login_label`.

La V1 utilise `AnonymousProvider` (jeton signé en cookie, hashé en base — aucune
PII). La V2 fournira un `FranceConnectProvider` (OIDC) implémentant **la même
interface** : aucun changement requis dans la logique de vote, les résultats ou
les templates. La page `/connexion` documente le mécanisme courant et affiche un
bouton FranceConnect désactivé (« bientôt disponible »).

## Confidentialité

Un vote ne stocke que `law_id`, `choice`, un jeton anonyme hashé, et un
horodatage. L'export en accès libre (`/loi/{slug}/export.csv` et `.json`) ne
contient **que** `law_id, choice, created_at` — aucun identifiant.

**Le jeton n'est créé qu'au moment du vote**, jamais à la simple consultation :
quelqu'un qui ne fait que lire une loi ne se voit poser aucun identifiant.

**L'export ne révèle jamais plus que la page de résultats.** La courbe
d'évolution n'est exposée qu'au-delà d'un volume minimal de votes
(`MIN_BUCKET_VOLUME`) pour empêcher toute ré-identification — et le même seuil
gouverne l'export :

- en dessous du seuil, l'export **ne contient aucun horodatage** ;
- au-dessus, les horodatages sont **arrondis** (au jour par défaut,
  `EXPORT_TIME_RESOLUTION`), à la résolution même de la courbe publiée ;
- les lignes sont triées sur les seules colonnes publiées (date arrondie, puis
  choix), et sur rien d'autre : l'ordre du fichier ne restitue donc pas la
  séquence d'arrivée des votes.

Publier l'instant exact de chaque vote reviendrait à publier la courbe que le
seuil retient, à la résolution choisie par le lecteur — et permettrait à qui sait
*quand* une personne a voté de lire *comment* elle a voté.

### Données démographiques agrégées (âge, sexe)

Le profil (tranche d'âge, sexe) est **facultatif** et n'est **jamais stocké par
votant·e**. À la première participation, il est immédiatement ajouté à des
**compteurs agrégés** (`demographic_tallies`) puis l'événement est oublié :

- la table de compteurs ne contient **ni jeton, ni identité, ni ligne par
  personne** — uniquement des totaux par `(loi, dimension, catégorie, choix)` ;
- aucun enregistrement ne relie jamais une personne à la fois à son profil et à
  son vote, donc **aucune donnée sensible au sens de l'article 9 du RGPD** n'est
  créée côté serveur ;
- chaque votant·e n'alimente que des compteurs *marginaux* (âge **ou** sexe),
  jamais une case croisée âge×sexe, ce qui garde les valeurs stockées élevées ;
- un drapeau booléen `demographics_recorded` sur le vote évite le double comptage
  lors d'un changement de vote (le profil n'est compté qu'une fois) ;
- si la personne **change son vote**, sa contribution est *déplacée* vers le
  nouveau choix au lieu de rester comptée sous l'ancien, de sorte qu'une
  répartition décrit toujours les votes tels qu'ils sont. Déplacer une
  contribution suppose de savoir *quelles catégories* elle concernait — ce que le
  serveur refuse justement de stocker : ces catégories reviennent donc du
  navigateur de la personne, dans un cookie signé (`vf_profil`), lu le temps
  d'une requête et jamais enregistré. Le cookie ne contient que les catégories,
  jamais le vote ; l'effacer laisse simplement l'ancienne contribution où elle
  était.

**Suppression de cellules (k-anonymat).** Une répartition n'est affichée ou
exportée que si **chacune** de ses cases atteint `MIN_CELL_COUNT` (10 par défaut).
Dès qu'une case est en dessous, toute la répartition est masquée — rien de
partiel n'est jamais publié, de sorte qu'une petite valeur ne peut être ni lue ni
recalculée par différence (avec les autres cases, un total, ou l'autre dimension).

**Dimensions disponibles.** `age`, `sexe`, `region` (13 régions métropolitaines +
Outre-mer). Chacune est un compteur **marginal indépendant** : on n'enregistre
jamais de croisement entre deux dimensions (p. ex. âge × région), ce qui
maintiendrait des cases trop petites. Ajouter une dimension = ajouter une valeur
de `dimension`, sans changement de schéma.

**Représentativité.** Chaque répartition affiche, à côté de la part observée, la
part nationale issue de l'INSEE — *uniquement* pour visualiser l'écart de
l'échantillon auto-sélectionné. Les résultats ne sont **jamais** repondérés.

Les valeurs de référence sont rafraîchies chaque année depuis l'**API Melodi**
ouverte de l'INSEE (jeu de données `DS_ESTIMATION_POPULATION`, estimations de
population au 1ᵉʳ janvier), par le service `insee-refresh` de docker-compose :

- **sexe**, **âge** et **région** (13 régions métropolitaines) sont calculés à
  partir des effectifs INSEE de l'année courante ; l'année source est affichée
  (« INSEE 2026 »).
- **âge** : les tranches du site sont des sommes de groupes quinquennaux INSEE,
  restreintes aux adultes (18+). Seule la tranche 18-24 est approchée (les 18-19
  ans = 2/5 du groupe 15-19) ; les autres sont des unions exactes.
- En cas d'indisponibilité de l'INSEE (ou de schéma modifié), le rafraîchissement
  échoue **sans rien écraser** : l'app continue de servir les dernières valeurs en
  cache, ou la constante de repli `INSEE_REFERENCE`. Aucune page n'appelle l'INSEE
  en direct. Rafraîchissement manuel : `python -m scripts.refresh_insee`.

**Taux de renseignement.** La page de résultats indique quelle proportion des
votant·es a partagé au moins une information de profil (dérivé du drapeau
`demographics_recorded`, sans stockage supplémentaire), pour lire les
répartitions en connaissance de cause.

## Import automatique des lois promulguées

Les lois ne sont plus saisies à la main : un service `assemblee-refresh` télécharge
chaque jour l'export **Dossiers législatifs** des données ouvertes de l'Assemblée
nationale (Licence Ouverte, sans authentification) et en extrait toutes les lois
effectivement **promulguées** — celles qui portent un numéro officiel et une
référence au Journal Officiel.

Ce que l'import récupère, pour chaque loi : le titre du dossier, le numéro
(`LOI n° 2026-532`), la date de promulgation, la date et le numéro du JO, la
référence NOR et le lien Légifrance.

**Rien n'est publié automatiquement.** Les lois importées atterrissent dans une
table de préparation, visible seulement dans l'administration (**Lois
promulguées**). L'administrateur décide loi par loi : « Créer la votation »
pré-remplit le formulaire avec les seuls éléments factuels (titre, référence
officielle, lien Légifrance), ou « Ignorer » écarte la loi. Les quatre blocs de
présentation neutre restent vides : ils sont rédigés et validés par une personne,
comme l'exige la charte du projet. Une décision d'ignorer une loi n'est jamais
réécrite par les imports suivants.

En cas d'indisponibilité de l'Assemblée nationale, l'import échoue **sans rien
écraser** ; l'administration continue d'afficher la dernière liste connue.
Import manuel : `python -m scripts.refresh_assemblee`.

Réglages : `AN_DOSSIERS_URL` (le `17` de l'URL est la législature, à changer une
fois par législature), `AN_IMPORT_SINCE_DAYS` (fenêtre **glissante** — 365 jours
par défaut — pour que l'import n'accumule pas d'arriéré au fil des années ; `0`
prend tout l'historique), `AN_IMPORT_SINCE` (date absolue facultative, qui
l'emporte sur la fenêtre) et `AN_REFRESH_INTERVAL` (cadence, 24 h par défaut).

Le service de fond réessaie indéfiniment en cas d'échec, ce qui le rendrait
silencieux si la source déménageait. Le tableau de bord de l'administration
affiche donc la date du dernier import réussi et alerte au-delà de
`AN_IMPORT_STALE_AFTER_HOURS`.

### Texte officiel des lois (Journal Officiel)

`app/legifrance.py` récupère le **texte officiel** d'une loi promulguée à partir
des archives quotidiennes du Journal Officiel publiées par la DILA
(`echanges.dila.gouv.fr`, Licence Ouverte, sans authentification). Le XML est lu
via `defusedxml` et les membres d'archive démesurés sont écartés sans être lus :
la source est publique et de confiance, mais quelques kilo-octets d'entités
imbriquées suffiraient sinon à saturer la mémoire.

```python
from app.legifrance import fetch_law_text

loi = fetch_law_text(nor="SFHX2609544L", jo_date=date(2026, 7, 28))
loi.title, loi.articles, loi.eli_url, loi.plain_text
```

La clé de jointure est le **NOR**, déjà stocké par l'import de l'Assemblée
nationale ; la date de publication au JO indique quelle archive quotidienne
télécharger. Les archives du matin (quelques centaines de ko) contiennent
généralement les textes du jour ; à défaut, celle du soir puis celle du lendemain
sont essayées.

Le texte n'est **jamais stocké** : une loi peut atteindre 230 000 caractères, et
il est récupérable à la demande en moins d'une seconde. La fonction renvoie aussi
l'URL ELI canonique (`.../eli/loi/2026/7/27/2026-668/jo/texte`), plus durable que
le lien hérité fourni par l'Assemblée nationale.

Limite connue : les archives quotidiennes ne remontent qu'à environ treize mois.
Au-delà, il faudrait l'archive de base complète (~1 Go) ou l'API Légifrance via
PISTE (qui exige un compte et des identifiants OAuth).

Réglage : `JORF_DUMPS_URL`.

### Brouillon IA (facultatif)

Depuis le formulaire d'une loi importée, le bouton « Brouillon IA » récupère le
texte officiel au Journal Officiel et propose un premier jet de **deux** blocs :

- « Ce que dit la loi » — résumé factuel du texte officiel ;
- « Ce que ça change en pratique » — conséquences concrètes, en langage accessible.

Le même bouton propose aussi les **arguments des partisans et des opposants**,
mais d'une manière radicalement différente : ils ne sont pas rédigés, ils sont
**cités**. Le Journal Officiel ne contient aucun matériau argumentatif, mais les
comptes rendus des séances publiques en contiennent — des positions défendues par
des parlementaires nommés, en séance, dans un document public.

Le modèle ne rédige rien : il renvoie l'identifiant d'une intervention et le
passage qu'il prétend en avoir copié. Le passage est ensuite **vérifié caractère
par caractère** contre l'intervention citée ; s'il ne s'y trouve pas, il est
écarté. Une citation inventée échoue à une comparaison de chaînes avant que
quiconque la voie.

L'attribution ne vient jamais du modèle : l'orateur, la date et le lien sont
recopiés depuis le compte rendu récupéré. Le modèle ne peut donc pas attribuer
une vraie citation à la mauvaise personne, puisqu'il ne fournit aucun nom.

**Le déséquilibre est signalé, jamais comblé.** Certains textes — les
ratifications de traités en particulier — sont adoptés sans opposition. Renvoyer
un bloc « contre » vide avec une note est exact ; fabriquer une objection pour
remplir la case ne l'est pas. L'écart est signalé à l'administrateur, qui
tranche.

**Les réserves sont distinguées des oppositions.** Un texte peut être adopté sans
qu'aucun orateur ne s'y oppose, tout en étant critiqué par des parlementaires qui
le votent malgré tout (« nous voterons ce texte, mais il manque les moyens »).
Classer ces critiques en « arguments des opposants » trahirait leurs auteurs ; les
taire priverait le lecteur d'une information réelle. Elles sont donc extraites
dans une catégorie à part, **affichées sous le formulaire à titre indicatif et
jamais pré-remplies** dans les blocs d'arguments : à l'administrateur de décider
s'il les reprend, en indiquant clairement la position de leur auteur. Elles
obéissent à la même vérification mot pour mot que les arguments.

**Le travail se fait en tâche de fond.** Un brouillon enchaîne le téléchargement
d'archives du Journal Officiel (plusieurs mégaoctets, fouillées à la recherche
d'un texte), un appel au modèle, la lecture des comptes rendus de séance, puis un
second appel : plusieurs minutes, bien au-delà de ce qu'un navigateur ou un proxy
inverse garde ouvert, et un fil d'exécution immobilisé pendant tout ce temps.
L'administration se contente donc de **programmer** le travail et répond
immédiatement ; le formulaire interroge `/admin/imports/{id}/brouillon/etat`
jusqu'au résultat, et l'on peut continuer à saisir pendant ce temps. Un délai
global (`AI_DRAFT_DEADLINE_SECONDS`) fait abandonner proprement plutôt que rester
suspendu.

Garde-fous :

- **Déclenchement manuel uniquement.** Aucun brouillon n'est généré par le cron ;
  la dépense suit les clics, jamais l'import.
- **Un brouillon n'est facturé qu'une fois.** Il est conservé sur la loi importée
  (colonne `draft`) et réaffiché tel quel à chaque réouverture, y compris après un
  redémarrage. Seul le bouton « Régénérer », qui demande confirmation, rappelle le
  modèle.
- **Vos saisies l'emportent.** Un champ déjà rempli à la main n'est jamais écrasé
  par un brouillon ; seuls les champs vides sont complétés.
- **Publication séparée.** Le brouillon reste sur le formulaire : il n'entre dans
  la table des lois que si vous l'enregistrez, après relecture.
- **Rien n'est publié.** Le formulaire arrive avec « Publiée » décoché, et un
  bandeau rappelle que le texte n'a pas été vérifié.
- **Taille mesurée avant l'appel** (`AI_MAX_INPUT_TOKENS`) : un texte hors norme
  est refusé plutôt que facturé silencieusement ; le corpus des interventions est
  tronqué à la même limite, et la troncature est signalée.
- **Les arguments sont facultatifs** : si les comptes rendus sont illisibles ou
  si le texte n'a fait l'objet d'aucune séance, les blocs factuels sont tout de
  même proposés, avec une note expliquant l'absence.
- **Sans `ANTHROPIC_API_KEY`, la fonctionnalité est absente** : le bouton ne
  s'affiche pas et aucun appel n'est possible.

Ordre de grandeur, mesuré : un brouillon complet enchaîne deux appels (résumé
factuel, puis sélection d'arguments) et les jetons de raisonnement sont facturés en
sortie. Une loi médiane revient à quelques dizaines de centimes ; la plus
volumineuse du corpus (programmation militaire : 232 000 caractères de texte et 86
interventions) a coûté **1,20 $** avant réglage. `AI_EFFORT=medium` et
`AI_MAX_SPEECHES` ramènent nettement ce plafond, et le cache garantit qu'une loi
n'est facturée qu'une fois.

Les arguments proposés sont un **vivier à trier**, pas ce qui sera publié :
`AI_ARGUMENTS_PER_SIDE` (défaut 5) est un plafond, atteint seulement si les débats
fournissent autant de passages réellement argumentés. Cinq arguments par camp font
beaucoup à lire pour un citoyen : réduisez avant d'enregistrer.

Réglages : `ANTHROPIC_API_KEY`, `AI_MODEL` (défaut `claude-opus-5`),
`AI_EFFORT` (défaut `medium` — à monter si les brouillons paraissent superficiels),
`AI_MAX_INPUT_TOKENS`, `AI_MAX_SPEECHES` (interventions envoyées au modèle),
`AI_ARGUMENTS_PER_SIDE`, `AI_RESERVES_MAX`, `AI_DRAFT_DEADLINE_SECONDS`.

Les séances où un texte a été discuté sont enregistrées à l'import (colonne
`seances`) : la référence de la réunion et celle du point à l'ordre du jour. Une
séance pouvant examiner plusieurs textes, c'est ce point qui garantit qu'aucun
argument portant sur une autre loi ne se glisse dans la présentation.

## Sécurité

Ce que le code tient, et pourquoi — l'essentiel est documenté à l'endroit
concerné, voici la carte :

- **Requêtes inter-sites** (`app/security.py`). Le vote *et* l'administration
  refusent un POST dont l'`Origin` (à défaut le `Referer`) n'est pas ce site. Pour
  l'administration, les identifiants HTTP Basic voyagent quoi qu'il arrive avec une
  requête inter-site ; pour le vote, `SameSite=Lax` retient bien un cookie existant
  mais n'empêche pas d'en créer un neuf — n'importe quelle page pourrait sinon
  transformer ses visiteurs en bulletins.
- **Limitation de débit** (`app/ratelimit.py`). L'adresse cliente est lue à
  `TRUSTED_PROXY_HOPS` entrées de la **fin** de `X-Forwarded-For` : les proxys
  ajoutent à la suite, donc tout ce qui est à gauche a été écrit par l'appelant.
  Lire à gauche revenait à laisser l'attaquant choisir sa propre clé de comptage.
  Le Caddyfile réécrit l'en-tête plutôt que de l'allonger, et la table de suivi est
  bornée : passé le plafond, on refuse au lieu de grandir.
- **Connexions administrateur.** Comparaison à temps constant *et* refus des
  tentatives : un couple d'identifiants statique sans verrouillage se devine à la
  vitesse du réseau. Au-delà de `ADMIN_RATE_LIMIT` échecs, *toute* tentative
  depuis cette adresse est refusée pendant la fenêtre — y compris avec le bon mot
  de passe. C'est délibéré dans les deux sens : compter les échecs sans les
  refuser ne ralentissait rien, et ne refuser que la tentative *réussie*
  transformait la limite en oracle (un 429 signifiait « ce mot de passe était le
  bon »). Seuls les échecs alimentent le compteur, donc un administrateur qui
  travaille n'est jamais ralenti.
- **URL stockées.** Les liens saisis dans l'administration (sources d'arguments,
  lien Légifrance) sont restreints à `http`/`https` — **refusés** avec un message,
  plutôt que vidés en silence — et re-filtrés au moment du rendu. Deux passages
  parce qu'il y a deux chemins : le formulaire protège ce qu'on y saisit, le filtre
  de gabarit protège aussi ce qui vient d'ailleurs (données de démonstration,
  migration, écriture directe en base). La V3 ouvre ce formulaire à tout
  utilisateur authentifié.
- **Modifications après publication.** Toute modification d'une loi *publiée* est
  enregistrée (`law_revisions`) et affichée publiquement sur la page de résultats,
  avec la valeur avant et après. Sans cela, un texte pouvait être réécrit pendant
  le vote et le résultat publié aurait décrit un texte que personne n'avait lu.
  Les brouillons non publiés ne sont pas tracés : personne n'a encore voté.
- **Suppression.** Une votation s'**archive** — elle quitte le site, ses votes sont
  conservés, l'opération est réversible. La suppression définitive n'est proposée
  que depuis les archives, sur une page de confirmation rendue par le serveur (et
  non par un attribut JavaScript, qui ne s'exécutait pas sans JS).
- **En-têtes.** L'application pose elle-même CSP stricte (`script-src 'self'`,
  aucun `unsafe-inline`), `nosniff`, `frame-ancestors 'none'` et
  `Permissions-Policy` fermée ; le Caddyfile pose les mêmes, plus HSTS, et
  l'emporte quand il est là. Les deux, parce que dépendre d'un proxy particulier
  signifiait qu'un déploiement derrière autre chose n'avait plus aucun en-tête,
  sans le moindre signal. Les pages ne sont par défaut **jamais mises en cache**
  (`private, no-store`) : la page d'une loi affiche le bulletin de la personne qui
  la lit. Seuls les fichiers statiques, empreintés par leur contenu, sont mis en
  cache durablement. Aucun script ni style en ligne dans les gabarits :
  les largeurs de barres et les confirmations passent par `static/app.js`, qui
  n'est qu'un rehaussement — tout fonctionne sans JavaScript.
- **Surface exposée.** En production, `/docs`, `/redoc` et `/openapi.json` sont
  désactivés : le schéma énumère toutes les routes d'administration.
- **Image.** Utilisateur non privilégié, aucune dépendance de développement,
  versions figées **et empreintées** (`requirements.lock`, installé avec
  `--require-hashes`), limites mémoire sur les services qui décompressent des
  archives publiques.
- **Identifiants ASCII.** `ADMIN_USERNAME` et `ADMIN_PASSWORD` doivent être en
  ASCII et l'application refuse de démarrer autrement : Starlette décode l'en-tête
  HTTP Basic en ASCII, donc un mot de passe accentué ne peut jamais correspondre —
  il n'aurait fait qu'enfermer dehors l'administrateur, sans explication.

## Journaux

Les journaux d'accès sont **désactivés** des deux côtés (uvicorn
`--no-access-log`, Caddy `level ERROR`) : ils associeraient une adresse IP à
l'horodatage d'un vote. `app/logging_config.py` fournit en échange une
journalisation applicative qui, par construction, ne consigne **jamais** une
adresse, un cookie ou un jeton de votant — seulement des identifiants de loi,
d'import, et des classes d'erreur.
