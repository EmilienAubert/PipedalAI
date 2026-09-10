# Tests et recette

## Automatisé

La suite vérifie : inventaire et liens symboliques autorisés, catalogue immuable, absence de chemins dans les capacités, contrats stricts, rejet d'un hash périmé, trois variantes ordonnées, compilation avec média, état LV2 Path, structure ZIP et forme des presets fournis.

```bash
PYTHONPATH=src PIPEDAL_AI_REFERENCE_PRESETS=/chemin/presets \
  python3 -m unittest discover -s tests -v
(cd tools/phase0 && python3 -m unittest discover -s tests -v)
```

## Recette matérielle unique

Cette étape ne peut pas être simulée de manière honnête sans votre Pi et votre interface audio.

1. Garder `allow_import=false`, générer les trois variantes et vérifier leur ZIP.
2. Importer manuellement la variante conservative dans PiPedal.
3. Baisser le niveau physique, charger le preset et confirmer qu'il n'y a ni silence ni pic dangereux.
4. Vérifier que le NAM/IR apparaît et que tous les contrôles sont restaurés après redémarrage PiPedal.
5. Mesurer `xrun`, charge CPU et température pendant 15 minutes avec une chaîne lourde.
6. Couper le réseau/PC : le son courant doit rester intact et un nouveau travail doit passer en `source=degraded`.
7. Seulement ensuite autoriser l'import puis l'activation automatique.

Critère de succès : trois presets importables, catalogue exact, aucune dépendance RTX pour l'audio live et activation récupérable.
