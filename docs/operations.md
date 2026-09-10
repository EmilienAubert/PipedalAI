# Exploitation et dépannage

## États d'un travail

`queued` → `running` → `completed` ou `failed`. `source=rtx` indique Ollama ; `source=degraded` indique la recette locale. Les artefacts ne sont enregistrés qu'après compilation et validation.

## Pannes normales

| Symptôme | Comportement | Action |
|---|---|---|
| RTX hors ligne | Trois variantes locales sont produites | Vérifier plus tard le PC ; aucun impact audio |
| Catalogue périmé | Proposition rejetée | Relancer le travail avec le catalogue actif |
| Hash NAM/IR différent | Compilation refusée | Refaire inventaire + import du catalogue |
| Charge élevée | Travail refusé | Attendre que le Pi soit moins chargé ; le live continue |
| Import interdit | Archive reste téléchargeable | Activer `allow_import` après recette matérielle |
| Activation échoue | Rollback vers l'ancien preset tenté | Vérifier PiPedal/WebSocket et activer manuellement |

## Sauvegarde

Arrêtez l'application ou utilisez l'API de sauvegarde SQLite avant copie. Sauvegardez la base et le dossier des artefacts. Les secrets et certificats se sauvegardent séparément avec des permissions restrictives.

## Mise à jour du catalogue

La révision active change après import d'un nouvel inventaire. Les anciens travaux restent traçables mais leurs propositions ne peuvent plus être recompilées contre le nouveau catalogue sans nouvelle génération. Cette contrainte est intentionnelle.
