# Mise en production

Une machine, Docker, un nom de domaine qui pointe dessus. Caddy obtient le
certificat TLS tout seul.

## Démarrer

```bash
git clone <dépôt> votations && cd votations
cp .env.example .env
```

Remplissez `.env` : il ne contient que ce sur quoi vous devez décider, chaque
réglage expliqué sur place. Deux valeurs à générer :

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(24))"   # ADMIN_PASSWORD
```

> **`SECRET_KEY` appartient aux données, pas à la machine.** Il signe le jeton de
> vote déposé dans le navigateur. Si vous restaurez une base existante sur un
> nouvel hôte avec une clé neuve, plus aucun cookie de votant n'est lisible :
> chacun reçoit un jeton neuf, qui ne correspond à aucune ligne, et **toute
> votation ouverte accepte un second bulletin de chaque personne ayant déjà
> voté**. En reprenant une base, reprenez la clé — ou passez l'ancienne dans
> `SECRET_KEY_FALLBACKS` (voir « Changer de clé » plus bas).
>
> `ADMIN_PASSWORD`, `POSTGRES_PASSWORD` et `SITE_DOMAIN` sont au contraire
> propres à chaque environnement et doivent changer.


> **`POSTGRES_PASSWORD` finit à l'intérieur d'une URL** (`DATABASE_URL`, composée
> dans le fichier compose). `token_urlsafe` ne produit que des lettres, des
> chiffres, `-` et `_`, qui s'y insèrent sans rien casser. Un mot de passe choisi
> à la main contenant `@`, `:`, `/`, `?`, `#` ou `%` couperait l'URL au mauvais
> endroit : la connexion échouerait avec un message qui ne parle pas du mot de
> passe. Gardez la commande ci-dessus, ou encodez les caractères spéciaux.

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f app
```

> **Un seul conteneur `app`.** `docker/entrypoint.sh` lance `alembic upgrade head`
> avant de démarrer le serveur. Avec une seule instance c'est exactement ce qu'il
> faut : le schéma est à jour avant la première requête, sans étape manuelle à
> oublier. Avec plusieurs répliques, elles démarreraient ensemble et appliqueraient
> la même migration en même temps — au mieux l'une échoue et redémarre en boucle,
> au pire deux `ALTER TABLE` se croisent. Le jour où la charge demande une seconde
> réplique, sortez d'abord la migration de l'entrypoint : un service `migrate` qui
> tourne une fois et s'arrête, dont les autres dépendent
> (`depends_on: { migrate: { condition: service_completed_successfully } }`).

## Ce que la pile fait tourner

| Service | Rôle |
|---|---|
| `caddy` | TLS et seule porte d'entrée ; pose les en-têtes de sécurité |
| `app` | l'application ; applique les migrations au démarrage |
| `db` | PostgreSQL |
| `assemblee-refresh` | importe les lois promulguées, une fois par jour |
| `insee-refresh` | rafraîchit les références de population, une fois par mois |
| `content-export` | un instantané du contenu éditorial par jour, cinq conservés, dans `./exports` |
| `db-backup` | un dump par jour, quatorze conservés |

Les trois derniers déclarent une sonde de santé fondée sur la date de leur
dernier succès : un service qui a cessé de fonctionner devient `unhealthy` au
lieu de rester « en cours d'exécution » sans rien produire.

```bash
docker compose -f docker-compose.prod.yml ps
```

## Mettre à jour

Après un merge sur `master`, approuvez le job `Deploy` dans l'onglet Actions. Il
lance `scripts/deploy.sh` sur le serveur, qu'on peut aussi lancer à la main :

```bash
git fetch origin
scripts/deploy.sh "$(git rev-parse origin/master)"
```

Le script refuse un commit absent de `master` ou un dépôt modifié localement,
télécharge l'image testée par la CI, fait un dump de la base, puis change de
version. Si l'application ne devient pas saine, il revient à la version
précédente. Une migration déjà appliquée n'est pas annulée : c'est le rôle du
dump.

Pour revenir à une version antérieure, relancez le job `Deploy` d'une exécution
plus ancienne.

Sans ghcr.io, construire sur place :

```bash
git checkout master && git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Les services de rafraîchissement utilisent l'image de `app`
(`votations-app:local`) et ne peuvent pas diverger d'elle.

### Mise en place du déploiement automatique

Sur le serveur :

- ne changez pas le nom du dossier : il fixe celui des volumes
  (`votations_pgdata`), et un dossier renommé démarre sur une base vide ;
- `origin` doit être en HTTPS (`git remote -v`) ;
- l'utilisateur doit lancer `docker` sans `sudo`.

Une clé SSH dédiée, limitée au script :

```bash
ssh-keygen -t ed25519 -N "" -C github-deploy-votations -f votations-deploy
```

À ajouter dans `~/.ssh/authorized_keys` sur le serveur, avec le chemin absolu :

```
restrict,command="/chemin/vers/votations/scripts/deploy.sh" ssh-ed25519 AAAA… github-deploy-votations
```

Sur GitHub, dans Settings :

- **Environments**, créer `production` : *Required reviewers* (vous),
  *Deployment branches* limité à `master`, le secret `DEPLOY_SSH_KEY` (la clé
  privée, à supprimer ensuite de votre machine) et les variables `DEPLOY_HOST`,
  `DEPLOY_USER`, `DEPLOY_PORT` (22 par défaut) et `DEPLOY_KNOWN_HOSTS` (sortie de
  `ssh-keyscan -p <port> <hôte>`).
- **Rules → Rulesets**, sur la branche par défaut : *Restrict deletions*,
  *Block force pushes*, *Require a pull request before merging* (0 approbation :
  GitHub refuse qu'on approuve sa propre PR) et *Require status checks to pass*
  avec `Code quality` et `Prod image`.
- **Actions → General** : *Require approval for all external contributors*.

Le paquet `votations` sur ghcr.io hérite de la visibilité publique du dépôt : le
serveur le télécharge sans identifiants.

### Les migrations qui créent un index

`docker/entrypoint.sh` applique les migrations au démarrage du conteneur, avant
que l'application ne réponde. Les migrations qui créent un index le font sans
`CONCURRENTLY` : un `CREATE INDEX` prend un verrou `SHARE` sur la table, qui
bloque les écritures — donc les votes — pendant sa construction.

Sans conséquence aujourd'hui, et pour deux raisons : la pile n'a qu'un conteneur
`app`, donc une mise à jour est de toute façon une courte interruption ; et sur
quelques milliers de votes un index se construit en quelques dizaines de
millisecondes. Ça cesse d'être vrai à quelques centaines de milliers de votes, où
la même migration tient la table plusieurs secondes.

Le jour où ce sera le cas, une migration qui touche `votes` doit passer en
`CONCURRENTLY` — ce qui ne peut pas se faire dans la transaction qu'Alembic
ouvre, et demande un `with op.get_context().autocommit_block():`. C'est une
décision à prendre au moment d'écrire la migration, pas après l'avoir appliquée :
`SELECT count(*) FROM votes` dit dans lequel des deux cas vous êtes.

### Les migrations irréversibles

La migration `7b1e4c9d2a60` (une empreinte du jeton de vote différente pour
chaque loi) ne s'annule pas. Une fois appliquée :

- une version antérieure du code ne démarre plus : son `alembic upgrade head` ne
  connaît pas cette migration. Ne relancez donc pas le job `Deploy` d'une
  exécution antérieure. Si `deploy.sh` revient de lui-même à la version
  précédente, elle ne démarrera pas non plus : restaurez le dump qu'il vient de
  prendre (`backup.sh restore`) ou redéployez la nouvelle version. Ne contournez
  pas l'erreur (en marquant la base à une révision antérieure, par exemple) :
  l'ancien code ne retrouverait plus les bulletins déjà déposés, et chacun
  pourrait revoter sur les votations ouvertes ;
- `alembic downgrade` refuse de passer tant que la table `votes` n'est pas vide :
  la rejouer ensuite hacherait les jetons une seconde fois ;
- les dumps antérieurs, dont celui que `deploy.sh` prend juste avant, gardent
  l'ancienne empreinte, commune à toutes les lois. Avec l'un d'eux et la base
  actuelle, on peut regrouper les votes d'un même navigateur, y compris ceux à
  venir. Une fois la nouvelle version validée, supprimez-les (volume `backups`
  et copies hors de la machine) sans attendre leur expiration ;
- dans la base elle-même, la migration suivante (`a3d9f6c1e872`) réécrit la
  table `votes` (`VACUUM FULL`), sans quoi les anciennes versions des lignes y
  resteraient lisibles. Le journal de PostgreSQL (WAL) peut en garder une copie
  jusqu'à son recyclage, quelques heures en général.

## Sauvegardes

Les lois importées se reconstruisent en relançant l'import. **Les votes ne se
reconstruisent pas** — ce sont les seules données irremplaçables de la plateforme.

```bash
# la liste
docker compose -f docker-compose.prod.yml exec db-backup sh /usr/local/bin/backup.sh list

# un dump immédiat
docker compose -f docker-compose.prod.yml exec db-backup sh /usr/local/bin/backup.sh dump

# restaurer (demande confirmation : cela écrase la base)
docker compose -f docker-compose.prod.yml exec db-backup \
  sh /usr/local/bin/backup.sh restore /backups/votations-AAAAMMJJ-HHMMSS.sql.gz
```

> **`restore` remplace la base, littéralement.** Elle vide le schéma avant de
> relire le fichier : un dump rejoué par-dessus un schéma existant s'arrêtait à la
> première table déjà présente, laissant une base à moitié restaurée. L'archive est
> vérifiée (`gunzip -t`) **avant** que quoi que ce soit ne soit effacé, donc un
> fichier tronqué se solde par un refus et une base intacte. Restaurez toujours
> dans une base jetable d'abord — voir « À ne pas oublier ».

### Hors de la machine

Les dumps quotidiens vivent sur le même disque que la base : ils protègent
contre « j'ai cassé les données », pas contre « la machine a disparu ».

Activez les sauvegardes automatiques de votre hébergeur (Hetzner, Scaleway et
OVH en proposent toutes). Un instantané est pris pendant que PostgreSQL tourne :
c'est l'équivalent d'une coupure de courant, ce dont il sait se relever — mais
restaurer un instantané ramène toute la machine, alors qu'un dump se relit ligne
à ligne. Les deux servent, et pas au même moment.

Vérifiez le nombre d'instantanés conservés par votre hébergeur : s'il en garde
moins que les quatorze jours de dumps locaux, c'est le dump qui rattrapera une
suppression remarquée tardivement.

## Déménager, ou repartir sur une base neuve

Le dump protège les votes mais ne se relit que dans le même schéma. Le contenu
éditorial — les lois, leur historique public de modifications, et les décisions
prises sur les lois importées — est exporté à part, une fois par jour, dans un
JSON lisible et portable :

Les fichiers sont écrits dans **`./exports`**, à côté du `docker-compose`. C'est
un montage lié et non un volume nommé : `cp`, `scp` ou `rsync` les atteignent
directement, sans passer par `docker cp`.

```bash
# la liste, depuis l'hôte
ls -lh exports/

# en emporter une copie
scp exports/votations-contenu-*.json ailleurs:/sauvegardes/

# un export immédiat
docker compose -f docker-compose.prod.yml exec content-export \
  python -m scripts.content export

# le recharger, ici ou ailleurs
docker compose -f docker-compose.prod.yml exec app python -m scripts.content import /exports/votations-contenu-AAAAMMJJ-HHMMSS.json
```

> **`EXPORT_UID`.** Le conteneur tourne sous votre compte plutôt que sous
> l'utilisateur de l'image, pour que les fichiers vous appartiennent. La valeur
> par défaut est `1000`, ce qu'est le premier compte d'un hôte Linux ordinaire ;
> si ce n'est pas le vôtre, mettez `EXPORT_UID` et `EXPORT_GID` dans `.env` :
>
> ```bash
> echo "EXPORT_UID=$(id -u)" >> .env
> echo "EXPORT_GID=$(id -g)" >> .env
> ```
>
> Le dossier `exports/` est versionné (vide) exprès : s'il n'existait pas au
> premier `up`, Docker le créerait au nom de root et le conteneur ne pourrait pas
> y écrire. En cas de doute, le service le dit et nomme le remède plutôt que
> d'échouer en silence.

L'import est **idempotent** : une loi dont le slug existe déjà est ignorée, pas
fusionnée. `--replace` demande l'inverse (que le fichier fasse autorité) et
**refuse de toucher une loi qui a été publiée**, même dépubliée depuis : la
remplacer réécrirait le texte sur lequel des gens ont pu voter sans rien inscrire
à l'historique public, ce qui rendrait fausse la page de résultats. Une telle loi
se modifie depuis `/admin`, où le changement est enregistré et affiché.

Ce que l'export **ne contient pas**, et pourquoi :

| | Pourquoi |
|---|---|
| les votes, les compteurs démographiques | c'est le rôle du dump ; une copie réduite ici serait prise pour une sauvegarde |
| les références INSEE | refabriquées chaque mois, avec une valeur de repli en attendant |
| les brouillons IA | regénérables, au prix d'un appel au modèle |

L'historique public des modifications voyage **dans le même fichier**, imbriqué
sous chaque loi : sans lui, le nouveau site affirmerait « aucune modification
depuis la publication » à propos de textes qui ont bougé.

Sur une base neuve, l'ordre est : migrations (automatiques), import du contenu,
puis attendre le premier passage de `assemblee-refresh`. Les décisions d'import
ne peuvent se rattacher qu'à des lois que le job a déjà vues, donc l'export le
dit et vous relancez la commande après son passage :

```
→ 12 loi(s) créée(s), 0 remplacée(s), 0 ignorée(s) ; 0 décision(s) réappliquée(s)
  72 décision(s) en attente : les lois correspondantes n'ont pas encore été vues
  par l'import quotidien. Relancez cette commande après son prochain passage.
```

### Changer de clé

`SECRET_KEY` signe le jeton de vote. Pour le remplacer sans déconnecter personne,
mettez l'ancienne clé dans `SECRET_KEY_FALLBACKS` (séparées par des virgules, la
plus ancienne d'abord) : elle continue de **vérifier** les cookies existants
pendant que la nouvelle **signe** les suivants. Retirez-la au bout d'un an, quand
les cookies qu'elle a signés ont expiré d'eux-mêmes.

```bash
SECRET_KEY=<la nouvelle>
SECRET_KEY_FALLBACKS=<l'ancienne>
```

> **Jamais une clé que vous croyez compromise.** Un repli est l'instruction de
> continuer à honorer les cookies signés avec elle : c'est exactement l'inverse
> de ce qu'une fuite demande. Une clé fuitée se remplace **sans repli**, en
> acceptant que chaque votant soit traité comme nouveau — c'est le but.
>
> Et il faut le dire clairement : une clé fuitée permet de forger des bulletins,
> pas de se faire passer pour l'administrateur (celui-ci tient à
> `ADMIN_PASSWORD`, pas à `SECRET_KEY`). Changer la clé arrête la forge, **elle
> ne l'annule pas** : les bulletins déjà déposés restent en base et rien ne les
> distingue des vrais. La question devient alors éditoriale — que publier sur les
> votations ouvertes pendant la fenêtre d'exposition — et la charte ne laisse pas
> l'option de se taire.

La production refuse de démarrer si un repli est un exemple, fait moins de
32 caractères, ou répète `SECRET_KEY` : une rotation qui garde la même clé n'a
rien tourné.

## À ne pas oublier

- **Remplir `.env`**, jusqu'au bout. Les mentions légales laissées vides
  s'affichent publiquement comme manquantes.
- **Activer les sauvegardes automatiques chez l'hébergeur.** Les dumps locaux ne
  survivent pas à la disparition de la machine ; c'est une case à cocher.
- **Répéter une restauration au moins une fois**, dans une base jetable. Une
  sauvegarde jamais restaurée est une hypothèse. Cela vaut aussi pour l'export de
  contenu : `python -m scripts.content import` dans une base vide, une fois.
- **Reprendre `SECRET_KEY` avec la base.** Une base restaurée sous une clé neuve
  rouvre chaque votation en cours à un second bulletin par personne.
- **Surveiller l'espace disque.** Archiver une votation ne libère rien — c'est
  voulu, les votes sont conservés. Seule la suppression définitive, depuis
  `/admin/archives`, rend de l'espace, et elle est irréversible.
- **Regarder le tableau de bord.** L'URL de l'export de l'Assemblée nationale
  porte le numéro de législature et changera ; l'import échouerait alors en
  silence. Le tableau de bord affiche la date du dernier import réussi et alerte
  au-delà de 48 h. Cette bannière veut dire : lisez les journaux du service.

  Le job ne s'acharne pas en attendant : après un échec il réessaie au bout d'une
  minute, puis double l'attente jusqu'à une heure (`docker/periodic.sh`). Un
  premier démarrage se rattrape donc en une minute, et une source qui a vraiment
  déménagé n'interroge plus un service public 1 440 fois par jour pour rien.

  ```bash
  docker compose -f docker-compose.prod.yml logs --tail=50 assemblee-refresh
  ```

- **Avec une adresse IPv6 (enregistrement AAAA), vérifier que Docker la gère.**
  Réglé par défaut, Docker fait passer les connexions IPv6 par `docker-proxy`,
  et Caddy voit alors l'adresse de sa passerelle pour tous les visiteurs IPv6 :
  ils partagent une seule limite de votes (`VOTE_RATE_LIMIT`) et se font refuser
  en masse. Activez IPv6 pour les réseaux Docker (documentation Docker « Use
  IPv6 networking »), ou ne publiez pas d'enregistrement AAAA. L'application
  compte une adresse IPv6 par bloc /64, celui d'une connexion.

- **Ne pas rallumer les journaux d'accès.** Ils sont désactivés des deux côtés
  (`--no-access-log`, Caddy en `level ERROR`) parce qu'ils associeraient une
  adresse IP à l'horodatage d'un vote — exactement le lien que la charte promet
  inexistant. L'application journalise ses propres échecs, sans jamais d'adresse,
  de cookie ni de jeton. Si vous ajoutez de la journalisation, tenez cette ligne.

- **Relire `/vie-privee` si le déploiement change.** Cette page décrit
  l'installation fournie ici. Ajoutez un outil de mesure d'audience, une police
  servie par un tiers ou des journaux d'accès, et elle devient fausse — et un
  traceur non exempté rendrait un vrai bandeau de consentement obligatoire.
