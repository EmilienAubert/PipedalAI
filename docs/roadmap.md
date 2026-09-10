# Limites et feuille de route

## MVP actuel

- texte vers trois chaînes série ;
- plugins, NAM et IR réellement inventoriés ;
- sélection locale simple des assets en mode dégradé ;
- pas de routage parallèle, automation MIDI générée, audio de référence ni apprentissage.

## Suite recommandée

1. Stabiliser le MVP par la recette matérielle et enregistrer CPU/latence/xruns par plugin.
2. Ajouter une intégration Tone3000 au Pi derrière `AssetProvider`, avec téléchargement contrôlé, scan puis nouvelle révision de catalogue. Aucune API Tone3000 n'est inventée dans ce dépôt.
3. Ajouter le banc de rendu interne au Pi avec TooB File Player et TooB Record Input ; ces plugins restent interdits aux presets live.
4. Introduire `AudioMatchRequest/1.0.0` : upload borné au Pi, référence opaque vers la RTX, séparation/analyse, classement NAM/IR.
5. Boucle d'optimisation : propositions RTX, rendus PiPedal, métriques, garde-fous de gain et budget d'itérations.
6. Préférences utilisateur locales puis index vectoriel RTX, toujours sans autorité d'écriture.

Chaque phase conserve l'API v1 existante ou ouvre une nouvelle version incompatible.
