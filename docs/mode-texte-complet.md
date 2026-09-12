# Mode texte complet

## Objectif

Le mode texte transforme une description musicale en trois presets PiPedal importables : `conservative`, `balanced` et `bold`. L'intelligence musicale s'exécute sur le PC RTX, mais le Raspberry Pi reste l'unique autorité sur le catalogue, les fichiers, les niveaux, la compilation et PiPedal.

Le mode est utilisable avant le calcul des empreintes audio. Les empreintes améliorent ensuite principalement le classement des NAM et des IR, sans changer le contrat de sécurité ni le format final.

## Répartition Pi / RTX

| Étape | Raspberry Pi | PC RTX |
|---|---|---|
| Réception de la demande | Reçoit le prompt et le profil de guitare | — |
| Catalogue | Fige la révision et le SHA-256, masque les chemins locaux | Ne voit que les capacités et identifiants opaques |
| Compréhension | Peut appliquer le mode dégradé | Produit un `ToneIntent` structuré |
| Recherche | Fournit les ressources réellement disponibles | `CandidateRetriever` classe plugins, NAM et IR |
| Planification | Recalcule les adaptateurs déterministes | Choisit un plan compact, les rôles et les candidats NAM/IR |
| Validation | Vérifie chaque plugin, port, valeur, ressource et limite | Ne peut imposer aucune décision au Pi |
| Production | Compile, relit et stocke les `.piPreset` | Ne produit pas de fichier PiPedal |
| Import/activation | Opérations locales explicites et contrôlées | Aucun accès à PiPedal |

## Pipeline d'une demande

1. L'utilisateur saisit un prompt et sélectionne éventuellement un profil de guitare.
2. Le Pi crée un travail lié à la révision et au SHA-256 du catalogue actif.
3. Le Pi envoie au service RTX le prompt, le profil et un `CatalogCapabilitySet` dépourvu de chemins locaux.
4. Ollama interprète la demande sous la forme d'un `ToneIntent` versionné `pipedal-ai.tone-intent/1.0.0`. Le contrat strict couvre `gain`, `dynamics`, `spectrum`, `space`, `modulation`, `delay`, `style`, `chain_constraints`, `guitar` et un niveau de confiance ; les valeurs psychoacoustiques sont bornées et les champs inconnus sont refusés.
5. `CandidateRetriever` établit une liste courte à partir des seules capacités reçues. Il utilise les métadonnées disponibles et, lorsqu'elles existent, les empreintes `AudioFingerprint`.
6. Le planificateur génère un `MusicalPlan` court, sans dictionnaires LV2. Les
   adaptateurs construisent trois `PresetSpec` liés au même catalogue et au même
   objectif, puis le Pi recalcule leurs réglages et valide les types de capture :
   - `conservative` : chaîne courte, niveaux prudents et charge réduite ;
   - `balanced` : meilleure interprétation globale du prompt ;
   - `bold` : interprétation plus marquée, mais toujours conforme aux limites annoncées.
7. Le Pi traite les réponses RTX comme des données non fiables. Il refuse un catalogue périmé, un identifiant inconnu, une mauvaise ressource, une valeur hors plage, un plugin réservé au rendu ou une chaîne trop coûteuse.
8. Le Pi résout localement les `asset_id`, recalcule leurs empreintes SHA-256, compile les archives `.piPreset`, puis les relit avant publication atomique.
9. L'utilisateur peut télécharger ou importer une variante. L'activation reste une action distincte jusqu'à la fin de la recette matérielle.

L'API stable reste `POST /api/v1/proposals/text`. Une route `POST /api/v1/intents/text` peut exposer le `ToneIntent` à des fins de diagnostic ; elle ne doit pas être nécessaire au fonctionnement normal du Pi.

## Où se trouve l'intelligence

Le modèle n'est pas chargé de respecter la syntaxe interne de PiPedal par intuition. Sa valeur est dans quatre décisions musicales :

- traduire une phrase libre en objectifs sonores cohérents ;
- déterminer les familles d'effets utiles et celles qui seraient superflues ;
- classer les NAM et IR susceptibles d'atteindre la cible ;
- choisir les compromis de chaîne et les NAM ; les réglages LV2 proviennent des
  adaptateurs, puis éventuellement d'une petite recherche mesurée sur le banc.

Par exemple, « blues chaud, crunch léger, très dynamique, graves fermes et petite room » implique de préserver l'attaque, de limiter la compression, de ne pas confondre crunch léger et high-gain, de contrôler les graves avant ou après l'ampli et de garder une réverbération courte et discrète. Le modèle peut conclure qu'un NAM crunch suffit et qu'une pédale de drive n'est pas nécessaire.

Le Pi conserve toutes les décisions objectives : existence des éléments, compatibilité des ressources, bornes de paramètres, politique CPU, sécurité des niveaux et droit d'importer ou d'activer.

## Fonctionnement avant les empreintes audio

Le premier mode RTX complet peut fonctionner avec :

- les noms et catégories issus de l'inventaire local ;
- les ports, plages et valeurs par défaut LV2 ;
- le profil de guitare et de micro ;
- les métadonnées locales ou Tone3000 déjà connues ;
- les règles de compatibilité, notamment la distinction entre NAM d'ampli seul et capture contenant déjà un cabinet lorsqu'elle est connue.

Cette version est immédiatement utile, mais la sélection d'un NAM reste partiellement sémantique. Une description incomplète ou fantaisiste associée à un fichier peut dégrader le classement. Tone3000 améliore le contexte ; son intégration complète ne bloque ni la génération ni le calcul des empreintes.

En l'absence de RTX ou en cas de réponse invalide, le Pi utilise le générateur local déterministe. Son analyse français/anglais construit aussi un `ToneIntent` minimal avec les mêmes bornes, afin que le reste du pipeline reste testable. L'audio live et le preset actuellement actif ne dépendent jamais du réseau.

## Fonctionnement avec les empreintes audio

Une `AudioFingerprint` versionnée `pipedal-ai.audio-fingerprint/1.0.0` accueille les mesures de rendus standardisés. L'analyse WAV livrée maintenant fournit des niveaux, une répartition spectrale et des indices provisoires de dynamique. Les valeurs de gain, compression et sensibilité ne deviennent des mesures NAM fiables qu'après comparaison appariée entre la DI d'entrée et plusieurs rendus calibrés ; l'outil actuel les marque donc comme base de classement, pas comme vérité acoustique définitive.

`FingerprintIndex` conserve ces données sur le PC RTX et les indexe par SHA-256 de l'asset. Une empreinte est donc réutilisable si le nom ou le chemin change, et invalidée si le contenu du NAM ou de l'IR change.

Les métadonnées Tone3000 peuvent être liées sur le Pi uniquement avec les identifiants exacts du tone et du modèle :

```bash
export TONE3000_ACCESS_TOKEN='...'
pipedal-ai --config config/pi.toml tone3000-enrich ast_... --tone-id 123 --model-id 456
```

Cette commande ne télécharge aucun modèle. Elle enrichit la révision locale, et la RTX ne reçoit que ces métadonnées et des identifiants opaques dans les capacités.

Avec cet index, `CandidateRetriever` combine :

- la proximité entre le `ToneIntent` et les mesures audio ;
- les métadonnées musicales ou matérielles disponibles ;
- la compatibilité NAM/IR ;
- le coût et les contraintes de la chaîne ;
- ultérieurement, les préférences apprises de l'utilisateur.

Le banc est d'abord alimenté hors ligne à partir d'un manifeste de WAV validé. Le rendu automatique de tous les NAM dans PiPedal ne doit être activé qu'après la recette matérielle, car TooB File Player, TooB Record Input, les accès disque et les changements de chaîne ne doivent jamais compromettre le jeu live. Le protocole de constitution des fichiers est décrit dans [Jeu de DI de référence](jeu-di-reference.md).

## Installation d'Ollama sur le PC RTX

Ollama peut fonctionner sous Linux, Windows ou un environnement compatible avec l'accélération NVIDIA. Utilisez l'installateur correspondant au système du PC, puis vérifiez que le pilote NVIDIA et la RTX sont visibles avant de poursuivre. Sous Windows, l'application Ollama peut fournir le service en arrière-plan ; sous Linux, il peut être lancé comme service utilisateur ou système. La configuration PiPedal AI ne doit dépendre d'aucun de ces modes de démarrage.

Téléchargez le modèle configuré, par exemple :

```bash
ollama pull qwen3:14b
ollama list
```

Vérifiez ensuite l'API locale d'Ollama :

```bash
curl http://127.0.0.1:11434/api/tags
```

Ollama doit rester lié à `127.0.0.1`. Seul le service PiPedal AI RTX est exposé au Pi.

Installez l'application dans un environnement Python isolé, copiez `config/rtx.example.toml` vers un fichier local non versionné, puis définissez le même jeton que sur le Pi :

```bash
python -m venv .venv
.venv/bin/python -m pip install .
export PIPEDAL_AI_RTX_TOKEN='secret-long-et-aleatoire'
.venv/bin/pipedal-ai-rtx --config config/rtx.toml
```

Sous Windows PowerShell, utilisez les exécutables équivalents dans `.venv\Scripts` et définissez la variable d'environnement dans la session ou le compte de service. Le modèle, l'URL Ollama, le délai et la température restent réglables dans `rtx.toml`.

## Sécurité d'exploitation

- N'exposez jamais Ollama au réseau local ou à Internet.
- Autorisez le port RTX uniquement depuis l'adresse du Pi, par pare-feu et `allowed_cidrs`.
- Utilisez un jeton long dans `PIPEDAL_AI_RTX_TOKEN` ; ne l'enregistrez ni dans Git ni dans SQLite.
- Employez mTLS pour l'installation durable : certificat client sur le Pi, certificat serveur sur la RTX et autorité privée.
- N'ajoutez aucun endpoint de shell, d'exécution de commande ou de transfert de chemin local.
- Limitez la taille des requêtes et les délais. Une réponse trop lente, malformée ou liée à un ancien catalogue déclenche le repli local.
- Gardez `allow_activation=false` tant que la recette avec la vraie interface audio n'est pas terminée.
- Lancez l'indexation et les rendus hors des répétitions et concerts. Ils ne font pas partie du chemin audio temps réel.

## Recette du mode texte

### Avant les empreintes

1. Régénérez l'inventaire sur le Pi et importez une nouvelle révision si les plugins, NAM ou IR ont changé.
2. Démarrez Ollama puis le service RTX ; contrôlez `/api/v1/health` depuis le Pi.
3. Créez au moins un profil par configuration réelle de guitare et micro, avec un `input_trim_db` mesuré prudemment.
4. Lancez un jeu fixe de prompts couvrant clean, edge-of-breakup, crunch, high-gain, fuzz, sons secs, chorus/delay et ambiance courte/longue.
5. Pour chaque prompt, vérifiez que les trois travaux sont attribués à `source=rtx`, qu'ils partagent le catalogue actif et que les archives sont générées sans correction manuelle.
6. Importez manuellement `conservative`, puis `balanced` et `bold`, volume physique abaissé. Vérifiez le routage, les ressources, les niveaux et la restauration des paramètres après redémarrage de PiPedal.
7. Coupez Ollama, puis le réseau du PC. Le preset actif doit continuer à fonctionner et une nouvelle demande doit terminer avec `source=degraded`.
8. Modifiez le catalogue entre requête et résultat lors d'un test contrôlé : le Pi doit refuser la proposition périmée.

Critère de passage : plusieurs familles sonores donnent trois presets importables, aucun élément absent n'est inventé, aucune interruption audio ne survient et le mode dégradé reste disponible.

### Après les empreintes

1. Validez le manifeste DI et calculez les empreintes uniquement pour un petit lot de NAM connu.
2. Relancez exactement les mêmes prompts et conservez les choix obtenus avant/après indexation.
3. Vérifiez que les assets inchangés réutilisent leur empreinte et qu'un SHA-256 nouveau force un nouveau calcul.
4. Évaluez à l'aveugle la fidélité au prompt, la dynamique, le bruit, le niveau et la préférence globale.
5. Étendez le lot seulement si le classement améliore réellement les résultats et si le temps d'indexation est maîtrisé.

Le succès n'est pas « l'IA a produit un JSON », mais « la variante classée première correspond mieux au prompt, s'importe sans intervention et reste sûre sur le matériel réel ».

## Intervention demandée à l'utilisateur

Pour terminer le mode texte avec sélection audio des NAM, il faut fournir :

1. un jeu de DI guitare conforme au protocole [Jeu de DI de référence](jeu-di-reference.md) ;
2. les profils exacts des guitares et micros utilisés ;
3. une courte séance sur le Pi pour autoriser et observer les premiers rendus ;
4. une évaluation simple des trois variantes sur un jeu de prompts stable ;
5. si possible, l'origine ou les identifiants Tone3000 des NAM locaux, sans que cela bloque l'indexation.

Le code peut préparer l'ingestion, le classement et les contrats sans le matériel. Les décisions sur le niveau d'entrée, la charge acceptable, les xruns et la qualité musicale exigent en revanche le Pi, l'interface audio et une écoute humaine.

## Limites du mode texte

- Un prompt ne contient pas une cible audio unique : plusieurs chaînes peuvent être également plausibles.
- Une empreinte mesure le comportement avec le jeu de DI et les niveaux choisis ; elle n'est jamais une vérité universelle sur le NAM.
- Le vocabulaire subjectif comme « crémeux », « énorme » ou « vintage » nécessite un apprentissage et peut varier selon l'utilisateur.
- Une similarité de mesures ne garantit pas une préférence musicale.
- Les métadonnées Tone3000 peuvent être absentes, communes à plusieurs captures ou subjectives.
- Sans rendu de la chaîne complète, les interactions entre drive, NAM, IR et EQ ne sont qu'estimées.
- L'activation automatique ne doit pas être considérée comme prête avant les tests de niveau, CPU, température, disque et perte réseau sur le Pi réel.
