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
  models/            Law, Vote (aucune donnée personnelle stockée)
  crud/              laws, votes, results (agrégats simples + export anonyme)
  auth/              Couche d'authentification enfichable (voir ci-dessous)
  routes/            public, admin, auth
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
