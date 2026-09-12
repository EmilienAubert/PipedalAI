# API version 1

Toutes les routes Pi sous `/api/v1` demandent `X-PiPedal-AI-Key`. La route RTX de proposition demande `Authorization: Bearer …`. Les CIDR sont aussi contrôlés.

## Pi

| Méthode | Route | Résultat |
|---|---|---|
| `GET` | `/health` | Santé sans secret, aucune donnée sensible |
| `GET` | `/api/v1/status` | Catalogue, charge, disponibilité RTX/PiPedal |
| `GET` | `/api/v1/catalog/capabilities` | Capacités sans chemin local |
| `GET/POST` | `/api/v1/profiles` | Lire/créer un profil guitare |
| `DELETE` | `/api/v1/profiles/{id}` | Supprimer un profil inutilisé |
| `POST` | `/api/v1/jobs/text` | Créer un travail asynchrone |
| `GET` | `/api/v1/jobs` | Historique récent |
| `GET` | `/api/v1/jobs/{id}` | État et trois artefacts |
| `GET` | `/api/v1/artifacts/{id}/download` | Télécharger un `.piPreset` |
| `POST` | `/api/v1/artifacts/{id}/import` | Import explicite dans PiPedal |
| `POST` | `/api/v1/artifacts/{id}/activate` | Activation explicite et vérifiée |
| `GET` | `/api/v1/di` | Jeux DI et annotations |
| `POST` | `/api/v1/di/import` | Manifeste + WAV en base64, validation et copie locales |
| `POST` | `/api/v1/feedback` | Retour explicite sur une variante d'un travail terminé |
| `POST` | `/api/v1/bench/evaluate` | Séance asynchrone confirmée hors live ; HTTP 202 |
| `GET` | `/api/v1/bench` | Historique récent des séances |
| `GET` | `/api/v1/bench/{id}` | État, erreur et rapport détaillé |
| `GET` | `/api/v1/bench/previews/{candidate_id}` | WAV de préécoute authentifié et hash vérifié |
| `GET` | `/api/v1/bench/{id}/presets/{variant}` | Preset exactement rendu, hash vérifié |
| `POST` | `/api/v1/bench/{id}/presets/{variant}/import` | Import explicite du preset mesuré, sans activation |

Exemple de création :

Les vues de travaux comprennent `fallback_reason` (nul sans repli). Un travail terminé
avec `source: "degraded"` et `error: null` est un succès du moteur local, pas de l'IA RTX.
La raison du repli reste consultable même lorsque les trois artefacts sont disponibles.

```json
{
  "prompt": "son blues chaud et dynamique, petite room",
  "profile_id": null,
  "auto_import": false,
  "auto_activate": false
}
```

## RTX

| Méthode | Route | Résultat |
|---|---|---|
| `GET` | `/api/v1/health` | État service et Ollama |
| `POST` | `/api/v1/intents/text` | `ToneIntent` strict pour diagnostic |
| `POST` | `/api/v1/proposals/text` | Un `ProposalSet` strict de trois variantes |
| `POST` | `/api/v1/audio/analyze-pair` | Analyse DI/rendu corrélée aux hashes et au catalogue |

Les schémas publiés sont dans `schemas/api-v1`. Toute évolution incompatible crée `/api/v2` et de nouvelles chaînes `schema_version`; les champs inconnus sont refusés.

`PlanDraft` est un contrat interne de génération Ollama, pas une nouvelle route réseau.
Le service RTX valide ses identifiants travail/catalogue avant la construction du
`ProposalSet`. Les contrats publics de proposition et de preset restent en version 1.

Depuis 0.8.0, `MusicalPlan/1.0.0` est le contrat interne par défaut. Le LLM choisit
une courte chaîne de rôles/plugins/fichiers et jusqu'à trois NAM compatibles.
Il ne produit pas de paramètres LV2. Le `ProposalSet` public ajoute un
`decision_report` facultatif ; `RTXProposalRequest` ajoute `preferences`, facultatif.
Le Pi recalcule les réglages des adaptateurs et vérifie le hash des capacités.
Une modification des connaissances ou des métadonnées invalide le travail.

Création d'une séance :

```json
{"job_id":"job_…","set_id":"ma_guitare","di_id":"dynamics","maintenance_confirmed":true,"optimize":true}
```

L'analyse RTX utilise `AudioPairRequest/1.0.0` : `request_id`, `catalog`,
`di_sha256`, `render_sha256`, `di_wav`, `render_wav`. Les deux derniers sont des
WAV base64 (32 Mio décodés maximum chacun). Aucun champ de chemin distant,
exécution ou commande PiPedal n'existe. L'analyse s'effectue en mémoire/temporaire
privé RTX, avec une seule analyse simultanée. Les requêtes de contrôle gardent une
limite de 2 Mo sur le Pi et 4 Mio sur la RTX ; seules les routes audio ont un plafond
supérieur. Les CIDR et secrets restent obligatoires sur le LAN.
