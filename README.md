# Votations Françaises

Plateforme citoyenne pour donner son avis sur les lois votées par le Parlement,
inspirée des votations suisses.

Développé avec l'aide de Claude Code.

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

Pour la production, voir [DEPLOY.md](DEPLOY.md).

## Tests

```bash
docker compose -f docker-compose.dev.yml run --rm app pytest
docker compose -f docker-compose.dev.yml run --rm app ruff check .
docker compose -f docker-compose.dev.yml run --rm app ruff format --check .
docker compose -f docker-compose.dev.yml run --rm app mypy
```

Les tests tournent sur un vrai PostgreSQL, et le schéma est construit par les
migrations (pas par `create_all`). `tests/test_migrations.py` vérifie que le
schéma migré correspond aux modèles et que les migrations s'annulent.

La CI lance ces commandes sur chaque pull request, puis teste la pile de
production avec `scripts/ci_smoke.sh`, utilisable aussi en local.

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
lecture) et n'en stocke qu'une empreinte différente pour chaque loi : la base ne
permet pas de regrouper les votes d'un même navigateur. Aucun bouton de
connexion n'est affiché.

FranceConnect n'est pas accessible à ce projet pour des raisons juridiques (voir
`CLAUDE.md`).

## Import des lois

Le service `assemblee-refresh` télécharge chaque jour l'export des dossiers
législatifs de l'Assemblée nationale et garde les lois promulguées : titre,
numéro, dates, NOR, séances et résultat du scrutin final. Les lois importées
apparaissent dans l'administration, où l'on crée une votation ou ignore la loi.
Le tableau de bord signale un import trop ancien (`AN_IMPORT_STALE_AFTER_HOURS`).

Réglages : `AN_DOSSIERS_URL` (le `17` est le numéro de législature),
`AN_IMPORT_SINCE_DAYS` (365 par défaut, `0` pour tout l'historique),
`AN_IMPORT_SINCE`, `AN_REFRESH_INTERVAL`. Import manuel :
`python -m scripts.refresh_assemblee`.

`app/legifrance.py` récupère le texte officiel d'une loi dans les archives
quotidiennes du Journal Officiel (DILA), à partir de son NOR. Le texte n'est pas
stocké, et les archives ne remontent qu'à environ treize mois
(`JORF_DUMPS_URL`).

## Brouillons IA (facultatif)

Sur le formulaire d'une loi importée, « Brouillon IA » propose :

- un premier jet de « Ce que dit la loi » et « Ce que ça change en pratique », à
  partir du texte officiel ;
- des arguments pour et contre, cités depuis les comptes rendus de séance. Le
  modèle renvoie l'identifiant d'une intervention et un passage, vérifié
  caractère par caractère ; l'orateur, la date et le lien viennent du compte
  rendu ;
- une phrase qui nomme le sujet du débat.

Le brouillon est généré en tâche de fond, stocké pour ne pas être payé deux fois,
et ne remplit que les champs vides. Sans `ANTHROPIC_API_KEY`, le bouton
n'apparaît pas.

Réglages : `ANTHROPIC_API_KEY`, `AI_MODEL`, `AI_EFFORT`, `AI_MAX_INPUT_TOKENS`,
`AI_MAX_SPEECHES`, `AI_ARGUMENTS_PER_SIDE`, `AI_DRAFT_DEADLINE_SECONDS`.

## Sécurité

- **Requêtes inter-sites** : les POST sont refusés si `Origin` (ou à défaut
  `Referer`) n'est pas le site. En production, un script doit envoyer
  `-H "Origin: https://<votre domaine>"`.
- **Limites de débit** : l'adresse est lue depuis la fin de `X-Forwarded-For`
  (`TRUSTED_PROXY_HOPS`).
- **Administration** : HTTP Basic, comparaison à temps constant, blocage après
  `ADMIN_RATE_LIMIT` échecs. Identifiants en ASCII uniquement.
- **Liens** : seuls `http` et `https` sont acceptés, à la saisie et à l'affichage.
- **Historique public** : dès sa première publication, toute modification d'une
  loi est enregistrée (même dépubliée entre-temps) et affichée sur la page de
  résultats.
- **Suppression** : une votation s'archive (réversible). La suppression
  définitive se fait depuis les archives.
- **En-têtes** : CSP stricte (`script-src 'self'`, rien en ligne), `nosniff`,
  `frame-ancestors 'none'`, `Permissions-Policy`. Caddy ajoute HSTS.
- **Cache** : `private, no-store` par défaut ; les pages identiques pour tout le
  monde sont cacheables.
- **Image** : utilisateur non privilégié, dépendances figées et vérifiées par
  hash. `scripts/relock.sh` met à jour `requirements.lock`.
- En production, `/docs`, `/redoc` et `/openapi.json` sont désactivés.

## Accessibilité

Contraste AA (WCAG) dans les deux thèmes, focus visible, lien d'évitement,
tableaux avec `scope`, description textuelle de la courbe, barres de résultats
masquées aux lecteurs d'écran, `prefers-reduced-motion` respecté. Aucun audit
RGAA n'a encore été fait.

## Journaux

Pas de journaux d'accès (uvicorn `--no-access-log`, Caddy ne garde que les
erreurs, sans IP). Les journaux de l'application ne contiennent ni adresse, ni
cookie, ni jeton, et les erreurs SQL n'incluent pas leurs paramètres
(`hide_parameters=True`).

## Licence

Code publié sous licence [GNU AGPL v3.0](LICENSE) ou toute version ultérieure.
Si vous faites tourner une version modifiée accessible en ligne, vous devez en
fournir le code source à ses utilisateurs.

Les données de vote exportées par le site sont publiées sous
[Licence Ouverte 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/)
(Etalab) : réutilisation libre, y compris commerciale, en citant la source.
