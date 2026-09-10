# Contribuer à PiPedal AI

Le Raspberry Pi reste l’unique autorité du système. Toute contribution doit préserver cette frontière : la RTX propose, le Pi valide et compile.

## Flux de travail

1. Créer une branche depuis `main`.
2. Faire un changement ciblé et documenté.
3. Exécuter les deux suites de tests.
4. Ouvrir une pull request ; ne jamais inclure de secret, clé privée, base locale ou configuration active.

```bash
python -m pip install ".[dev]"
python -m unittest discover -s tests -v
(cd tools/phase0 && python -m unittest discover -s tests -v)
```

## Règles de sécurité

- aucune écriture RTX directe dans PiPedal ;
- aucun chemin local ou accès shell transmis à la RTX ;
- tout travail reste lié à une révision et un hash de catalogue ;
- les NAM, IR et presets de test ne sont publiés que si leur licence autorise la redistribution ;
- l’audio live ne dépend jamais de la disponibilité du réseau.
