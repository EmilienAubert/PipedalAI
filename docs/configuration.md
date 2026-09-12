# Configuration

## Pi

| Section | Clé | Rôle |
|---|---|---|
| `server` | `allowed_cidrs` | Réseaux clients acceptés |
| `storage` | `database` | Base phase zéro enrichie des tables applicatives |
| `storage` | `artifact_root` | Répertoire privé des `.piPreset` |
| `storage` | `upload_root` | Racine PiPedal des NAM/IR, lecture seule |
| `rtx` | `base_url` | URL HTTPS du PC |
| `rtx` | `ca_file`, certificat et clé | mTLS Pi → RTX |
| `pipedal` | `allow_import` | Autorise l'upload local, désactivé par défaut |
| `pipedal` | `allow_activation` | Autorise le changement de preset, désactivé par défaut |
| `policy` | `max_chain_length` | Limite déterministe de chaîne |
| `policy` | `max_load_per_cpu` | Seuil d'admission des travaux de fond |
| `policy` | `min_free_mb` | Réserve disque minimale |

## RTX

`ollama.model` sélectionne le modèle. `max_retries`, `max_plugin_candidates` et
`max_assets_per_role` bornent le coût et la quantité de données envoyée au planificateur.
`output_format` vaut `schema` par défaut ; `json` est une compatibilité explicite, jamais
un repli automatique après HTTP 400. `think` accepte un booléen, `low`, `medium`, `high`
ou `auto` (option omise). `num_ctx` et `num_predict` bornent contexte et sortie.
`diagnostics_directory` active des traces privées bornées à 32 échanges ; absent, aucune
réponse brute n'est sauvegardée. Leurs prompts et métadonnées peuvent être sensibles.
Le délai Pi doit englober les deux étapes et toutes leurs reprises ; voir
[la procédure Pi/Windows](mise-a-jour-v073.md).
`fingerprints.index_path` désigne l'index local RTX ; un index absent démarre vide.
L'appel étant isolé derrière `OllamaClient` et une API `/api/v1`, un autre moteur pourra
le remplacer sans modifier le Pi.

## Secrets

- `PIPEDAL_AI_API_KEY` protège l'API/UI du Pi ; vide uniquement pour développement local ;
- `PIPEDAL_AI_RTX_TOKEN` complète le mTLS et doit être identique des deux côtés ;
- `TONE3000_ACCESS_TOKEN` n'est lu que pendant une commande explicite d'enrichissement ;
- ne placez aucune valeur secrète dans les fichiers TOML versionnés.
