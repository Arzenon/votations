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

Ajoutez ensuite, **dans le même `.env`**, trois variables que `docker compose`
lit lui-même (elles ne font pas partie des réglages de l'application, d'où leur
absence de `.env.example`) :

```bash
SITE_DOMAIN=votations.exemple.fr
POSTGRES_PASSWORD=<généré comme ci-dessus>
TRUSTED_PROXY_CIDR=172.16.0.0/12   # facultatif, défaut correct pour Docker
```

Sans elles, `docker compose up` s'arrête immédiatement en nommant celle qui
manque, plutôt que de démarrer sur une valeur vide.

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f app
```

L'application refuse de démarrer sur des secrets d'exemple, sur un fuseau
inconnu, sur des identifiants non ASCII ou sur un seuil d'anonymat désactivé.
Un démarrage qui échoue dit lequel : lisez le message plutôt que de le contourner.

Les migrations sont appliquées au démarrage de `app`. Créez ensuite votre
première loi depuis `/admin`.

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

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs --tail=30 app
```

Les conteneurs de rafraîchissement partagent l'image de l'application ; les
reconstruire aussi, d'où le `--build` sans nom de service.

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

Le script refuse d'écrire une sauvegarde vide ou tronquée, et refuse de démarrer
un dump s'il reste moins de 500 Mo libres : une partition pleine arrête PostgreSQL,
donc les votes en cours.

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
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.content import /exports/votations-contenu-AAAAMMJJ-HHMMSS.json
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
fusionnée. `--replace` demande l'inverse — que le fichier fasse autorité — et
**refuse de toucher une loi publiée** : la remplacer réécrirait le texte sur
lequel des gens ont voté sans rien inscrire à l'historique public, ce qui rendrait
fausse la page de résultats. Une loi publiée se modifie depuis `/admin`, où le
changement est enregistré et affiché.

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

  ```bash
  docker compose -f docker-compose.prod.yml logs --tail=50 assemblee-refresh
  ```

- **Ne pas rallumer les journaux d'accès.** Ils sont désactivés des deux côtés
  (`--no-access-log`, Caddy en `level ERROR`) parce qu'ils associeraient une
  adresse IP à l'horodatage d'un vote — exactement le lien que la charte promet
  inexistant. L'application journalise ses propres échecs, sans jamais d'adresse,
  de cookie ni de jeton. Si vous ajoutez de la journalisation, tenez cette ligne.

- **Relire `/vie-privee` si le déploiement change.** Cette page décrit
  l'installation fournie ici. Ajoutez un outil de mesure d'audience, une police
  servie par un tiers ou des journaux d'accès, et elle devient fausse — et un
  traceur non exempté rendrait un vrai bandeau de consentement obligatoire.
