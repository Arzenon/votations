# Votations Françaises

Plateforme citoyenne pour donner son avis sur les lois votées par le Parlement.
La vision et les principes du projet (transparence, neutralité, aucune donnée
personnelle) sont décrits dans `CLAUDE.md`.

## Stack

- **Backend** : FastAPI (Python 3.14)
- **Frontend** : templates Jinja2 rendus côté serveur, HTMX en amélioration
  progressive (tout fonctionne sans JavaScript)
- **Base de données** : PostgreSQL (SQLAlchemy 2 + Alembic)

## Démarrage

Tout tourne dans Docker. Le conteneur applique les migrations au démarrage.

```bash
docker compose -f docker-compose.dev.yml up --build
```

- Accueil : http://127.0.0.1:8000/
- Administration : http://127.0.0.1:8000/admin (HTTP Basic, `admin` /
  `change-me`, voir `docker-compose.dev.yml`)

Le site démarre vide. Pour ajouter quelques lois d'exemple :

```bash
docker compose -f docker-compose.dev.yml run --rm app python -m scripts.seed
```

Le script est idempotent. `SEED_ON_START=1` le lance à chaque démarrage.

Le fichier s'appelle `docker-compose.dev.yml` et non `docker-compose.yml` pour
qu'un `docker compose up` sans `-f` ne lance jamais la pile de développement par
erreur. Pour la production, voir [DEPLOY.md](DEPLOY.md).

## Tests et qualité

```bash
docker compose -f docker-compose.dev.yml run --rm app pytest
docker compose -f docker-compose.dev.yml run --rm app ruff check .
docker compose -f docker-compose.dev.yml run --rm app ruff format --check .
docker compose -f docker-compose.dev.yml run --rm app mypy
```

Les tests tournent sur un vrai PostgreSQL, et le schéma est construit par les
migrations (pas par `create_all`). `tests/test_migrations.py` vérifie que le
schéma migré correspond aux modèles et que les migrations s'annulent.

Les outils de test sont dans l'étage `dev` de l'image. L'image de production
(`target: base`) n'en contient aucun, tourne sans privilèges et installe les
versions figées de `requirements.lock`.

## Architecture

```
app/
  main.py            Application FastAPI, pages d'erreur, robots.txt, /sante
  config.py          Réglages
  db.py              Moteur SQLAlchemy, sessions
  security.py        Contrôle d'origine des POST, URL autorisées
  ratelimit.py       Limites de débit en mémoire
  profile_cookie.py  Profil démographique facultatif, stocké dans le navigateur
  templating.py      Jinja, filtres, empreinte des fichiers statiques
  charts.py          Courbe d'évolution en SVG
  models/            Law, Vote, ImportedLaw, compteurs démographiques
  crud/              Accès aux données
  auth/              Authentification (interface AuthProvider)
  routes/            public/, admin/, auth
  assemblee.py       Import des lois promulguées (Assemblée nationale)
  scrutins.py        Résultats des scrutins de l'Assemblée
  legifrance.py      Texte officiel (Journal Officiel, DILA)
  debates.py         Interventions en séance (comptes rendus)
  insee.py           Données de référence INSEE
  ai.py, drafting.py Brouillons IA
  templates/, static/
alembic/             Migrations
docker/              Point d'entrée et tâches périodiques
scripts/             Imports, export du contenu, sauvegarde, seed, mises à jour
tests/
```

## Authentification

Le code de vote ne dépend que de l'interface `AuthProvider`
(`app/auth/base.py`). Le seul fournisseur actuel, `AnonymousProvider`, dépose un
jeton aléatoire dans un cookie signé au moment du vote (jamais à la simple
lecture) et n'en stocke que le hash. Aucun bouton de connexion n'est affiché :
voter ne demande pas de compte.

FranceConnect n'est pas accessible à ce projet, pour des raisons juridiques :
voir `CLAUDE.md`, section « Authentication ».

## Données et vie privée

Un vote contient la loi, le choix, le hash du jeton et la date. L'export public
(`/loi/{slug}/export.csv` et `.json`) contient seulement `law_id`, `choice` et
`created_at`.

L'export ne révèle jamais plus que la page de résultats :

- en dessous de `MIN_BUCKET_VOLUME` votes (50 par défaut), la courbe d'évolution
  est masquée et l'export ne contient aucune date ;
- au-dessus, les dates sont arrondies au jour (`EXPORT_TIME_RESOLUTION`) ;
- les lignes sont triées par date arrondie puis par choix, pour ne pas révéler
  l'ordre d'arrivée des votes.

Des dates exactes permettraient à quiconque sait quand une personne a voté de
retrouver son vote.

### Profil démographique

L'âge, le sexe et la région sont facultatifs et ne sont jamais stockés par
personne. Ils sont ajoutés une seule fois à des compteurs agrégés
(`demographic_tallies`), un par dimension, sans croisement entre dimensions.

- Un vote indique seulement si un profil a été compté
  (`demographics_recorded`). Le bulletin est une seule transaction et la ligne
  du vote est verrouillée, pour qu'un double clic ne compte pas le profil deux
  fois.
- Si la personne change son vote, sa contribution est déplacée vers le nouveau
  choix. Les catégories reviennent d'un cookie signé (`vf_profil`) qui ne
  contient pas le vote. Sans ce cookie, l'ancienne contribution reste où elle
  est.
- Une répartition n'est affichée que si chaque case atteint `MIN_CELL_COUNT`
  (10 par défaut). Sinon, toute la répartition est masquée.
- Pendant le vote, la page affiche un instantané qui n'avance qu'une fois par
  heure au plus et par paliers d'au moins `MIN_BUCKET_VOLUME` profils. Sans cela,
  comparer deux affichages de la page révélerait le profil et le vote d'une
  personne.
- Chaque répartition affiche aussi la part nationale INSEE, pour montrer l'écart
  avec la population. Les résultats ne sont jamais repondérés.

Les données INSEE (jeu `DS_ESTIMATION_POPULATION` de l'API Melodi) sont
rafraîchies chaque mois par le service `insee-refresh`. En cas d'échec, les
dernières valeurs sont conservées. Rafraîchissement manuel :
`python -m scripts.refresh_insee`.

## Import des lois

Le service `assemblee-refresh` télécharge chaque jour l'export des dossiers
législatifs de l'Assemblée nationale et garde les lois promulguées : titre,
numéro, date, référence au Journal Officiel, NOR, séances où le texte a été
débattu, et résultat du scrutin final quand il y en a un.

Rien n'est publié automatiquement. Les lois importées apparaissent dans
l'administration, où l'on choisit de créer une votation ou d'ignorer la loi. Une
loi ignorée le reste lors des imports suivants. En cas d'échec, la dernière liste
connue est conservée, et le tableau de bord signale un import trop ancien
(`AN_IMPORT_STALE_AFTER_HOURS`).

Réglages : `AN_DOSSIERS_URL` (le `17` est le numéro de législature),
`AN_IMPORT_SINCE_DAYS` (365 par défaut, `0` pour tout l'historique),
`AN_IMPORT_SINCE` (date fixe, prioritaire), `AN_REFRESH_INTERVAL`. Import manuel :
`python -m scripts.refresh_assemblee`.

### Texte officiel

`app/legifrance.py` récupère le texte d'une loi dans les archives quotidiennes
du Journal Officiel publiées par la DILA, à partir de son NOR et de sa date de
publication. Le texte n'est pas stocké. Les archives ne remontent qu'à environ
treize mois. Réglage : `JORF_DUMPS_URL`.

## Brouillons IA (facultatif)

Sur le formulaire d'une loi importée, « Brouillon IA » propose :

- un premier jet de « Ce que dit la loi » et « Ce que ça change en pratique », à
  partir du texte officiel ;
- des arguments pour et contre, **cités** et non rédigés. Le modèle renvoie
  l'identifiant d'une intervention en séance et un passage ; le passage est
  vérifié caractère par caractère dans l'intervention, sinon il est écarté.
  L'orateur, la date et le lien viennent du compte rendu, jamais du modèle ;
- une phrase qui nomme le sujet du débat, sans prendre position.

Un déséquilibre entre les deux camps est signalé, jamais comblé. Les critiques
formulées par des partisans du texte ne sont pas classées comme arguments contre.

Le brouillon est généré en tâche de fond et le formulaire affiche son état. Il
n'est lancé que par un clic, stocké pour ne pas être payé deux fois, et ne fait
que préremplir les champs vides : une personne relit et enregistre. La taille du
texte est mesurée avant l'appel (`AI_MAX_INPUT_TOKENS`). Sans
`ANTHROPIC_API_KEY`, le bouton n'apparaît pas.

Coût mesuré : quelques dizaines de centimes pour une loi moyenne, 1,20 $ pour la
plus longue (loi de programmation militaire) avec `AI_EFFORT=medium`. Le défaut
est `high`.

Réglages : `ANTHROPIC_API_KEY`, `AI_MODEL`, `AI_EFFORT`, `AI_MAX_INPUT_TOKENS`,
`AI_MAX_SPEECHES`, `AI_ARGUMENTS_PER_SIDE`, `AI_DRAFT_DEADLINE_SECONDS`.

## Sécurité

- **Requêtes inter-sites** : les POST (vote et administration) sont refusés si
  `Origin` (ou à défaut `Referer`) n'est pas le site. En production, un POST sans
  aucun des deux est refusé ; un script doit envoyer
  `-H "Origin: https://<votre domaine>"`.
- **Limites de débit** : l'adresse est lue depuis la fin de `X-Forwarded-For`
  (`TRUSTED_PROXY_HOPS`). La limite est volontairement large, car beaucoup de
  personnes partagent une même adresse (réseaux mobiles).
- **Administration** : HTTP Basic, comparaison à temps constant, et toutes les
  tentatives sont refusées après `ADMIN_RATE_LIMIT` échecs. Identifiants en ASCII
  uniquement.
- **Liens** : seuls `http` et `https` sont acceptés, à la saisie puis à
  l'affichage.
- **Historique public** : toute modification d'une loi publiée est enregistrée et
  affichée sur la page de résultats.
- **Suppression** : une votation s'archive (réversible, votes conservés). La
  suppression définitive n'est possible que depuis les archives, après
  confirmation, et la page propose d'abord l'export.
- **En-têtes** : CSP stricte (`script-src 'self'`, rien en ligne), `nosniff`,
  `frame-ancestors 'none'`, `Permissions-Policy`. L'application les pose
  elle-même ; Caddy ajoute HSTS.
- **Cache** : `private, no-store` par défaut. Les pages identiques pour tout le
  monde sont mises en cache (une minute, trente secondes pour les résultats d'un
  vote ouvert, cinq minutes pour un vote clos).
- **Image** : utilisateur non privilégié, dépendances figées et vérifiées par
  hash (`--require-hashes`). `scripts/relock.sh` met à jour le lockfile.
- En production, `/docs`, `/redoc` et `/openapi.json` sont désactivés.

## Accessibilité

- Contraste AA (WCAG) vérifié sur chaque couple texte/fond, dans les deux thèmes.
- Anneau de focus visible et lien d'évitement.
- Tableaux avec `scope` et légendes.
- La courbe a une description textuelle et les chiffres sont dans un tableau.
- Les barres de résultats sont masquées aux lecteurs d'écran (le pourcentage est
  écrit à côté).
- `prefers-reduced-motion` est respecté.

Aucun audit avec un lecteur d'écran n'a encore été fait : ce n'est pas une
conformité RGAA vérifiée.

## Journaux

Les journaux d'accès sont désactivés (uvicorn `--no-access-log`, Caddy ne garde
que les erreurs, sans adresse IP) : ils associeraient une adresse IP à l'heure
d'un vote. Les journaux de l'application ne contiennent jamais d'adresse, de
cookie ni de jeton, et les erreurs SQL n'incluent pas leurs paramètres
(`hide_parameters=True`).
