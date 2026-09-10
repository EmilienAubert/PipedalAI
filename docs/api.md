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

Exemple de création :

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
| `POST` | `/api/v1/proposals/text` | Un `ProposalSet` strict de trois variantes |

Les schémas publiés sont dans `schemas/api-v1`. Toute évolution incompatible crée `/api/v2` et de nouvelles chaînes `schema_version`; les champs inconnus sont refusés.
