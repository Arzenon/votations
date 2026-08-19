# Mise en production

Ce document couvre ce qui ne peut pas être décidé dans le code : les secrets, la
machine, les sauvegardes. Il suppose un VPS sous Docker et un nom de domaine qui
pointe déjà vers lui.

Le point de départ : `docker-compose.yml` est le fichier de **développement**.
La production utilise `docker-compose.prod.yml`, qui ne monte pas le code, ne
recharge pas à chaud, ne publie pas PostgreSQL et n'insère jamais de lois
d'exemple.

---

## 1. Choisir l'hébergement

Quatre conteneurs applicatifs plus PostgreSQL et Caddy : prévoir **2 Go de RAM**.
Un gigaoctet suffit jusqu'au jour où une génération de brouillon et PostgreSQL se
croisent.

Le choix de l'hébergeur n'est pas neutre pour ce projet. La plateforme conserve
les votes de citoyens français sur un sujet français : « où sont stockées les
données » est une question à laquelle il vaut mieux avoir une réponse courte.
Un hébergeur français — **Scaleway** (Paris) ou **OVH** (Roubaix, Gravelines) —
coûte deux euros de plus par mois et se déclare publiquement. **Hetzner**
(Allemagne, Finlande) reste dans l'Union européenne et revient moins cher.

Ouvrir **80** et **443** uniquement. Ni 8000 ni 5432 ne doivent être joignables
depuis l'extérieur : Caddy est la seule porte.

---

## 2. Générer les secrets

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(24))"   # ADMIN_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(24))"   # POSTGRES_PASSWORD
```

`SECRET_KEY` signe le cookie de votant. Tant qu'il vaut la valeur d'exemple,
**n'importe qui peut forger un jeton et voter sans limite** : tous les
pourcentages publiés perdent leur sens. Le générer avant l'ouverture, jamais
après — le changer invalide les cookies existants, et les votants déjà venus sont
recomptés comme nouveaux.

Créer un `.env` **à côté de `docker-compose.prod.yml`**, jamais dans Git :

```ini
SITE_DOMAIN=votations.example.fr
SECRET_KEY=…
ADMIN_USERNAME=…
ADMIN_PASSWORD=…
POSTGRES_PASSWORD=…
# Facultatif : sans clé, le bouton « Brouillon IA » disparaît simplement.
ANTHROPIC_API_KEY=
```

Sur la clé Anthropic : la fournir par l'environnement de déploiement plutôt que
par un fichier posé à côté du code, et fixer un plafond de dépense sur un espace
de travail dédié dans la console.

L'application **refuse de démarrer** si `SECRET_KEY` ou `ADMIN_PASSWORD` sont
restés à leur valeur d'exemple, ou si `COOKIE_SECURE` n'est pas activé. Un oubli
provoque un arrêt bruyant, pas une mise en ligne silencieusement vulnérable.

---

## 3. Démarrer

```bash
git clone <dépôt> votations && cd votations
cp .env.example .env && $EDITOR .env          # valeurs de l'étape 2

docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f app
```

Les migrations s'appliquent au démarrage du conteneur (`docker/entrypoint.sh`).
C'est correct pour une instance unique. Avec plusieurs répliques, elles se
gêneraient au démarrage : en faire alors une étape explicite du déploiement.

Vérifier :

```bash
curl -s https://$SITE_DOMAIN/sante                       # "ok"
curl -s -o /dev/null -w "%{http_code}\n" https://$SITE_DOMAIN/admin   # 401
docker compose -f docker-compose.prod.yml ps             # tout "healthy"
```

Puis, une fois seulement, se connecter à `/admin` avec les identifiants choisis
et lancer un premier import :

```bash
docker compose -f docker-compose.prod.yml exec app python -m scripts.refresh_assemblee
```

---

## 4. Sauvegardes

Le service `db-backup` produit un dump par jour dans le volume `backups`, et en
conserve quatorze. Les lois importées se reconstruisent en relançant l'import ;
**les votes ne se reconstruisent pas**. Ce sont les seules données irremplaçables
de la plateforme.

```bash
# la liste
docker compose -f docker-compose.prod.yml exec db-backup sh /usr/local/bin/backup.sh list

# un dump immédiat
docker compose -f docker-compose.prod.yml exec db-backup sh /usr/local/bin/backup.sh dump
```

Le script refuse d'écrire une sauvegarde vide : si `pg_dump` échoue, ou si le
résultat ne contient ni schéma ni table `votes`, rien n'est conservé et le code
de sortie est non nul. Une sauvegarde silencieusement vide est pire que pas de
sauvegarde du tout.

### Copier hors de la machine

Une sauvegarde sur le VPS protège contre « j'ai cassé les données ». Elle ne
protège pas contre « le VPS a disparu ». Copier ailleurs, par exemple chaque
nuit :

```bash
# sur la machine distante, dans une tâche cron
rsync -az --delete \
  vps:/var/lib/docker/volumes/votations_backups/_data/ \
  /sauvegardes/votations/
```

### Répéter une restauration au moins une fois

Une sauvegarde jamais restaurée est une hypothèse.

```bash
docker compose -f docker-compose.prod.yml exec db-backup \
  sh /usr/local/bin/backup.sh restore /backups/votations-AAAAMMJJ-HHMMSS.sql.gz
```

La commande demande confirmation : elle écrase la base. Pour une répétition sans
risque, restaurer dans une base jetable et y pointer une instance de test.

---

## 5. Ce que les journaux ne doivent pas contenir

L'accès Uvicorn est désactivé (`--no-access-log`) et Caddy ne journalise que les
erreurs. Ce n'est pas un détail de confort : le journal d'accès enregistre
l'adresse IP de chaque requête, ce qui associerait des adresses aux horodatages
des votes — exactement le lien que la charte promet inexistant.

La limitation de débit garde ses compteurs en mémoire, pour quelques minutes, et
ne les écrit nulle part.

En cas d'ajout de journalisation : ne jamais y faire figurer de jeton de votant,
d'adresse IP, ni le contenu d'un brouillon.

---

## 6. Après la mise à jour du code

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build app
docker compose -f docker-compose.prod.yml logs --tail=30 app
```

Les conteneurs de rafraîchissement (`insee-refresh`, `assemblee-refresh`,
`db-backup`) partagent l'image de l'application : les reconstruire aussi après un
changement qui les concerne.
