# Limites et feuille de route

## MVP actuel

- texte vers trois chaînes série ;
- plugins, NAM et IR réellement inventoriés ;
- pipeline RTX en deux passes, sélection déterministe bornée et repli local ;
- contrat ToneIntent, métadonnées Tone3000 en lecture seule et index d'empreintes provisoire ;
- pas de routage parallèle, automation MIDI générée, audio de référence ni apprentissage.

## Suite recommandée

1. Stabiliser le MVP par la recette matérielle et enregistrer CPU/latence/xruns par plugin.
2. Relier les provenances Tone3000 exactes aux assets locaux ; tout téléchargement futur reste contrôlé, scanné et publié par le Pi.
3. Ajouter le banc de rendu interne au Pi avec TooB File Player et TooB Record Input ; ces plugins restent interdits aux presets live.
4. Introduire `AudioMatchRequest/1.0.0` : upload borné au Pi, référence opaque vers la RTX, séparation/analyse, classement NAM/IR.
5. Boucle d'optimisation : propositions RTX, rendus PiPedal, métriques, garde-fous de gain et budget d'itérations.
6. Préférences utilisateur locales puis index vectoriel RTX, toujours sans autorité d'écriture.

Chaque phase conserve l'API v1 existante ou ouvre une nouvelle version incompatible.
