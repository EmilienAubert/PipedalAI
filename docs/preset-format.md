# PresetSpec et format PiPedal

## PresetSpec v1

Un preset ne contient que des identifiants opaques issus du catalogue :

```json
{
  "schema_version": "pipedal-ai.preset-spec/1.0.0",
  "catalog": {"revision": 1, "sha256": "…64 caractères…"},
  "variant": "balanced",
  "name": "Blues chaud",
  "description": "Crunch dynamique avec room légère",
  "chain": [{
    "instance_id": "amp",
    "plugin_id": "plg_…",
    "bypass": false,
    "parameters": {"inputGain": 0},
    "resources": [{"role": "nam_model", "asset_id": "ast_…"}]
  }]
}
```

Le Pi retrouve URI, ports et fichiers. Il vérifie types, plages et compatibilité : `nam_model` pour TooB NAM, `cab_ir` pour TooB Cab IR et `reverb_ir` pour les convolutions TooB.

## Archive `.piPreset`

Le compilateur produit un ZIP comprenant :

- `bankFile.json`, banque à un preset et chaîne série ;
- `pluginsUsed.json`, métadonnées des plugins ;
- `media/<chemin relatif>`, copie vérifiée des NAM/IR nécessaires.

Les propriétés LV2 Path sont encodées dans `lv2State` et `pathProperties`, conformément aux presets de référence. L'archive interdit chemins absolus, `..`, doublons et dépassement de taille. Les dates ZIP sont fixes afin qu'une entrée identique produise des octets identiques.

L'import utilise l'endpoint local PiPedal `/var/uploadPreset`; l'activation passe ensuite par son WebSocket et vérifie `selectedInstanceId`. Cette intégration a été alignée sur le [dépôt source officiel de PiPedal](https://github.com/rerdavies/pipedal).
