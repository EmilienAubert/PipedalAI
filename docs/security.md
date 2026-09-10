# Sécurité

## Menaces traitées

- proposition RTX malformée ou compromise : validation stricte et catalogue lié par révision/hash ;
- lecture arbitraire de fichiers : la RTX ne transmet aucun chemin, le Pi résout seulement les `asset_id` inventoriés ;
- modification d'un asset après inventaire : taille, type de fichier, racine approuvée et SHA-256 sont revérifiés pendant la compilation ;
- traversée ou bombe ZIP : chemins et taille décompressée sont bornés puis l'archive est relue ;
- accès réseau non désiré : CIDR, secret constant-time, mTLS et pare-feu ;
- perturbation audio : aucune dépendance temps réel au PC et admission des travaux selon la charge/disque ;
- mauvaise activation : import et activation distincts, lecture de l'état et tentative de rollback.

## mTLS minimal

Créez une autorité privée, un certificat serveur RTX avec SAN correspondant à son IP/nom, et un certificat client Pi. La clé de CA et les clés privées doivent être en mode `600`. Le service RTX reçoit `--ssl-ca-certs`, ce qui exige un certificat client valide. Le Pi reçoit la CA, son certificat et sa clé via le TOML.

Ajoutez un pare-feu hôte : port 8091 accepté uniquement depuis le Pi ; port 8090 uniquement depuis vos postes du LAN ; port Ollama jamais exposé.

## Limites

Le contrôle CIDR s'appuie sur l'adresse TCP directe : ne placez pas le service derrière un proxy sans une politique explicite. L'API Pi peut être lancée en HTTP sur un LAN de test ; utilisez TLS ou un réseau administré avant de saisir la clé depuis un poste non fiable.
