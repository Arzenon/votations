# Votations Françaises

Plateforme citoyenne d'expression sur les lois françaises. Voir `CLAUDE.md` pour
la vision et les principes (transparence radicale, neutralité, aucune donnée
personnelle).

## Stack

- **Backend** : FastAPI (Python 3.12+)
- **Frontend** : templates Jinja2 rendus côté serveur + HTMX (progressif — tout
  fonctionne sans JavaScript)
- **Base de données** : PostgreSQL (SQLAlchemy 2 + Alembic).

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
```

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
  main.py            Fabrique de l'application FastAPI
  config.py          Réglages (DATABASE_URL, SECRET_KEY, identifiants admin…)
  db.py              Moteur SQLAlchemy + dépendance get_db
  models/            Law, Vote, ImportedLaw (aucune donnée personnelle stockée)
  crud/              laws, votes, results (agrégats simples + export anonyme), imports
  auth/              Couche d'authentification enfichable (voir ci-dessous)
  routes/            public, admin, auth
  assemblee.py       Import des lois promulguées (données ouvertes AN)
  templates/         base, home, law, results, search, connexion, admin/*
  static/            CSS minimal + htmx
alembic/             Migrations
tests/               pytest
```

### Le « joint » FranceConnect (préparé pour la V2)

Tout le code de vote ne dépend que de l'interface `AuthProvider`
(`app/auth/base.py`) :

- `get_voter_token(request, response)` → jeton **anonyme**, non identifiant,
  utilisé uniquement pour « un vote par loi ».
- `login_url()` / `logout_url()` / `is_authenticated()` / `login_label`.

La V1 utilise `AnonymousProvider` (jeton signé en cookie, hashé en base — aucune
PII). La V2 fournira un `FranceConnectProvider` (OIDC) implémentant **la même
interface** : aucun changement requis dans la logique de vote, les résultats ou
les templates. La page `/connexion` documente le mécanisme courant et affiche un
bouton FranceConnect désactivé (« bientôt disponible »).

## Confidentialité

Un vote ne stocke que `law_id`, `choice`, un jeton anonyme hashé, et un
horodatage. L'export en accès libre (`/loi/{slug}/export.csv` et `.json`) ne
contient **que** `law_id, choice, created_at` — aucun identifiant. La courbe
d'évolution n'est exposée qu'au-delà d'un volume minimal de votes
(`MIN_BUCKET_VOLUME`) pour empêcher toute ré-identification.

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
  lors d'un changement de vote (le profil n'est compté qu'une fois).

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
fois par législature), `AN_IMPORT_SINCE` (ignorer les promulgations antérieures ;
vide = tout l'historique) et `AN_REFRESH_INTERVAL` (cadence, 24 h par défaut).
