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

### Le réglage à ne pas se tromper : `TRUSTED_PROXY_HOPS`

La limitation de débit du vote est la seule barrière contre le bourrage d'urnes
scripté, et elle est indexée sur l'adresse cliente. Les proxys inverses
**ajoutent** leur observation à la suite de `X-Forwarded-For` : les entrées de
gauche sont donc celles que l'appelant a écrites lui-même. Le réglage dit à quelle
distance de la **fin** lire.

- Derrière le Caddy fourni (qui réécrit l'en-tête au lieu de l'allonger) : `1`.
- Avec un proxy supplémentaire devant (CDN, équilibreur de charge) : `2`, et ainsi
  de suite — comptez les sauts, pas les machines.
- Sans aucun proxy : `0`, l'en-tête est alors totalement ignoré.

Trop bas, des visiteurs légitimes partagent un compteur. Trop haut — ou en lisant
à gauche — n'importe qui obtient un compteur neuf à chaque requête en inventant
une adresse, et **la limite ne s'applique plus du tout**. Vérifiez après
déploiement : deux votes consécutifs portant un `X-Forwarded-For` forgé différent
doivent bien être comptés ensemble.

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

L'application journalise ses propres échecs (`app/logging_config.py`) : c'est ce
qui rend visible un import qui ne passe plus ou un brouillon qui échoue. Ce
journal ne contient, par construction, aucune adresse, aucun cookie et aucun
jeton — seulement des identifiants de loi et d'import et des classes d'erreur.

En cas d'ajout de journalisation : ne jamais y faire figurer de jeton de votant,
d'adresse IP, ni le contenu d'un brouillon.

---

## 6. Ce que l'export publie

`EXPORT_TIME_RESOLUTION` (défaut `day`) fixe la résolution des horodatages de
l'export en accès libre. En dessous de `MIN_BUCKET_VOLUME` votes, l'export
**n'en contient aucun** : c'est le seuil qui retient déjà la courbe d'évolution, et
un fichier horodaté le contournerait à la résolution choisie par le lecteur.

Ne passez à `exact` qu'en sachant ce que cela ouvre : qui sait *quand* une personne
a voté lit alors *comment* elle a voté.

---

## 7. Après la mise à jour du code

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build app
docker compose -f docker-compose.prod.yml logs --tail=30 app
```

Les conteneurs de rafraîchissement (`insee-refresh`, `assemblee-refresh`,
`db-backup`) partagent l'image de l'application : les reconstruire aussi après un
changement qui les concerne.

Les migrations sont appliquées par l'entrypoint au démarrage de `app`. La suite de
tests construit son schéma par ces mêmes migrations et vérifie qu'il correspond
aux modèles, donc une révision manquante est visible avant le déploiement, pas
pendant.

---

## 8. Surveiller que l'import tourne encore

Le service `assemblee-refresh` réessaie indéfiniment : si la source déménage — et
elle déménage, l'URL de l'export porte le numéro de législature — il échouerait en
silence. Le tableau de bord de l'administration affiche donc la date du dernier
import réussi et alerte au-delà de `AN_IMPORT_STALE_AFTER_HOURS` (48 h par
défaut). Une bannière y signifie : allez lire les journaux du service.

```bash
docker compose -f docker-compose.prod.yml logs --tail=50 assemblee-refresh
```
