# Générateur local sans RTX

Le générateur local assure le mode dégradé lorsque le service RTX est désactivé,
injoignable ou renvoie une réponse invalide. Il ne s'agit pas d'un modèle d'IA :
la sélection est déterministe, explicable et limitée au catalogue actif du Pi.

## Interprétation du texte

Le texte est normalisé en minuscules sans accents. Un vocabulaire français et
anglais extrait les intentions suivantes : niveau de saturation (`clean`,
`crunch`, `high_gain`), couleur (`warm`, `bright`, `dark`), famille d'ampli et
présence d'une ambiance. Une négation comme `sans reverb` ou `sec` l'emporte sur
la demande d'espace.

## Chaîne produite

Le générateur recherche, dans cet ordre logique :

1. TooB Input Stage ;
2. un drive adapté si le texte demande du crunch ou du high-gain ;
3. TooB Neural Amp Modeler et un fichier NAM classé par nom ;
4. TooB Parametric EQ Mono, avec une correction légère de couleur ;
5. TooB Freeverb si une ambiance est demandée ;
6. TooB Volume pour conserver une marge de sortie sûre.

Les priorités de drive sont explicites. Un son blues/crunch préfère
GxTubeScreamer, puis GxSD1, ClubDrive et GxRat. Un son high-gain préfère GxRat,
puis GxSD1, GxTubeScreamer et ClubDrive. L'ordre du catalogue n'influence plus
ce choix.

## Classement des NAM

Le nom de chaque NAM est comparé aux intentions. Les familles d'ampli ont le
poids le plus fort, puis le niveau de gain et enfin la couleur. Les conflits
évidents sont pénalisés : un modèle nommé `clean` est défavorisé pour une
demande high-gain, et un modèle `cranked` ou `metal` pour une demande clean.
En cas d'égalité, le nom puis l'identifiant assurent un résultat reproductible.

Ce classement ne remplace pas une analyse audio. Il exploite uniquement les
noms des fichiers réellement inventoriés.

## Trois variantes et sécurité

Les variantes conservative, balanced et bold modifient progressivement le
gain d'entrée, le seuil du gate, la quantité de drive, l'égalisation et le taux
de reverb. Les paramètres inexistants sont ignorés et toutes les valeurs sont
bornées par les métadonnées LV2 du catalogue.

Le Pi effectue ensuite la validation complète du `PresetSpec`, vérifie le hash
du NAM, compile l'archive et reste le seul composant autorisé à l'importer dans
PiPedal.
