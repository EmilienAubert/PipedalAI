# Banc DI, caractérisation et optimisation

Le Pi choisit les fichiers, construit la chaîne, rend dans PiPedal et valide les
exports. La RTX reçoit uniquement deux WAV, leurs hashes et une référence de
catalogue ; elle analyse ces données et ne reçoit aucun accès shell ou commande
PiPedal. Cette phase ne sépare pas une guitare d'un morceau commercial.

## Fonctionnement et limites

La restauration porte sur la configuration sérialisée du pedalboard. Elle ne
garantit pas les buffers audio transitoires d'un looper ou les queues d'effets en
mémoire ; termine et sauvegarde ces prises avant une séance de banc.

| Élément | Comportement |
|---|---|
| Texte seul | Trois fichiers importables, aucune mesure automatique ni changement du son live |
| Banc | Séance hors live confirmée, chaîne temporaire File Player → effets → Record Input |
| Sortie physique | Atténuation du master à −96 dB ; enregistrement avant ce master |
| Restauration | Pedalboard courant complet, y compris modifications non sauvegardées |
| Changement utilisateur | Arrêt du banc ; le nouveau pedalboard utilisateur n'est pas remplacé |
| CPU / décrochages | Abandon si seuil CPU audio dépassé ou compteur d'underruns augmenté |
| Réseau / RTX indisponible | Échec de la mesure sans repli local ; restauration locale après chaque rendu |
| Concurrence | Un verrou local empêche deux processus de rendre simultanément |
| Caractérisation | Même DI à −6, 0, +6 dB ; niveau trop fort omis, au moins deux niveaux nécessaires |
| Empreinte | Spectre, enveloppes et réponse aux niveaux, avec contexte DI/NAM/IR/calibration/descripteur |
| Optimisation | Trois chaînes initiales puis petits voisins de gain NAM et shelf aigu ; budget limité |
| Préécoute | Égalisation RMS avec plafond de crête −3 dBFS, pas une mesure LUFS |
| Export mesuré | Seulement la chaîne exactement rendue, sans écrêtage détecté et sous −1 dBFS |

Les indices de chaleur, brillance, compression, sensibilité et saturation sont
des approximations explicites. La distance au prompt n'est pas une probabilité
de ressemblance à un artiste. Les mesures NAM amp-only dépendent de l'IR associée ;
elles sont conservées mais ne classent pas isolément les NAM dans cette version.
Les profils full-rig compatibles contribuent au classement du mode texte avec
un poids borné. Les déclarations de type NAM restent des métadonnées, pas une
preuve acoustique. Une phase stéréo opposée ne doit pas annuler l'analyse d'énergie.

## Activation et première recette

La génération automatique utilise seulement les plugins dotés d'un adaptateur
vérifié (`knowledge.py`), principalement TooB et Axis Face pour une fuzz explicitement
demandée. Les autres plugins restent inventoriés et utilisables manuellement dans
PiPedal. Une demande exigeant un effet sans adaptateur peut être refusée ou passer
en mode local explicitement signalé ; une catégorie LV2 ne suffit pas à inventer
ses réglages. Les rôles wah/pitch/compresseur restent à étendre et vérifier.

Arrête PiPedal AI, puis ajoute cette section à `config/pi.toml` si elle manque :

```toml
[bench]
enabled = true
di_root = "./data/di"
output_root = "./data/bench"
max_renders = 3
max_audio_cpu_percent = 75
tail_seconds = 2
track_directory = "shared/audio/Tracks"
record_directory = "shared/audio/Audio Recordings"
# nam_input_calibration_dbu = -6.0 # Remplacer uniquement par une mesure réelle connue
```

Le banc utilise les plugins TooB inventoriés ; il échoue si File Player ou Record
Input manque. Les dossiers audio doivent correspondre aux dossiers partagés de
ta version PiPedal. Si elle utilise `Tracks` et `Audio Recordings` directement à la
racine d'`audio_uploads`, change uniquement ces deux réglages. Pas de chemin absolu
ou `..`. Vérifie le chemin affiché par PiPedal et les droits d'écriture du compte
qui lance PiPedal AI. Les IR usine liées symboliquement conservent la politique
de confiance explicite du compilateur.

Relance les services AI sur les deux machines. Génère un nouveau travail texte
terminé, sélectionne ta DI, confirme la séance hors live et clique « Comparer /
optimiser sur la DI ». Avec `max_renders=3`, seuls les trois originaux sont évalués.
Vérifie que ton pedalboard original revient avec ses réglages, puis écoute les
préécoutes dans le navigateur. Les previews et presets sont téléchargés avec la
clé API ; ni les chemins arbitraires ni les fichiers modifiés ne sont servis.

Après cette recette, tu peux mettre `max_renders=15` pour mesurer les trois chaînes
et jusqu'à quatre voisins par variante. Le meilleur score éligible de chaque
variante est exporté. Des profils proches sont signalés ; cela ne garantit pas
trois sons perceptuellement distincts. Les fichiers originaux restent disponibles.

## Commandes locales Pi

Le manifeste est décrit dans [le guide DI](jeu-di-reference.md). Copie les WAV et
leur manifeste sur le Pi, dans un dossier d'entrée de ton choix, puis :

```bash
.venv/bin/pipedal-ai --config config/pi.toml di-import --manifest /chemin/di/manifest.json
.venv/bin/pipedal-ai --config config/pi.toml di-list
.venv/bin/pipedal-ai --config config/pi.toml bench-evaluate \
  --job-id job_IDENTIFIANT_DE_L_INTERFACE --set mon_jeu_di --maintenance-confirmed
```

La commande d'évaluation exige un travail stocké dans l'interface/API, pas seulement
les fichiers produits par `diagnose-rtx`. Pour ignorer les voisins, ajoute
`--no-optimize`. Le `set_id` et les `di_id` viennent du manifeste. Sans `--di-id`,
la prise marquée `purpose="dynamics"` est choisie, sinon la première prise.

Première caractérisation :

```bash
.venv/bin/pipedal-ai --config config/pi.toml characterize \
  --set mon_jeu_di --nam-limit 1 --maintenance-confirmed
```

Pour les amp-only, fournis une IR installée : `--cab-ir ast_IDENTIFIANT`. Les
full-rig ne reçoivent pas une seconde IR. Un modèle de type inconnu est mesuré
sans cabinet ajouté ; son caractère reste incertain et doit être écouté.
Les mesures restent associées à cette IR, cette DI et ces réglages. La calibration
NAM du banc doit être la même que celle du profil utilisé pour générer ; sinon les
empreintes sont exclues du classement pour ce profil. Une valeur par défaut n'est
pas une calibration mesurée de ton matériel.

Indexer davantage de modèles, avec reprise du cache :

```bash
.venv/bin/pipedal-ai --config config/pi.toml characterize \
  --set mon_jeu_di --all --cab-ir ast_IDENTIFIANT --maintenance-confirmed
```

Chaque appel respecte `max_renders`. Relance la même commande pour continuer ;
les contextes déjà présents sont réutilisés. Les profils sont bornés à 16 contextes
par asset. Le cache est invalidé par une autre DI, NAM, IR, calibration ou version
de descripteur/hôte. Les vecteurs et leur provenance sont stockés sur le Pi et
transmis dans les capacités à la RTX ; il n'y a pas de base vectorielle distante
à maintenir pour ce catalogue. Relance un travail texte après enrichissement :
les anciens instantanés de connaissances deviennent périmés pour une nouvelle mesure.

## Interruption et récupération

Chaque rendu écrit `data/bench/render-…/restore.json` **avant** le changement de
chaîne. La restauration normale fonctionne aussi après l'échec d'une connexion
WebSocket ou l'annulation du service. Si le processus est brutalement tué ou si
PiPedal est injoignable, le journal armé conserve le pedalboard original.

Une fois PiPedal accessible, restaure explicitement depuis le Pi :

```bash
.venv/bin/pipedal-ai --config config/pi.toml bench-recover \
  --journal data/bench/render-IDENTIFIANT/restore.json
```

La commande refuse d'écraser un autre pedalboard sélectionné depuis. Tu peux aussi
choisir manuellement un preset connu dans PiPedal. Le redémarrage de PiPedal AI
n'applique jamais silencieusement un ancien journal. Ne démarre pas une séance
live avec le banc en cours. Une séance terminée laisse fonctionner le moteur
PiPedal même si Windows disparaît.

## Retours d'écoute

Pour chaque variante initiale, l'interface accepte « Ma préférée », « Trop saturée »,
« Trop brillante », « Trop sombre » et « Bruit / craquements ». Les retours sont
conservés dans SQLite et ajoutent un petit bonus/malus de classement aux mêmes
fichiers, uniquement pour le même profil de guitare. Sans profil nommé, aucun goût
universel n'est déduit. Il s'agit d'un apprentissage explicite simple, pas d'un
réentraînement du LLM ni d'une correction automatique prouvée des défauts audio.
