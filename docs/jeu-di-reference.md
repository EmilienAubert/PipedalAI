# Jeu de DI guitare de référence

## But

Le jeu de DI est un ensemble de prises de guitare sans effet. Les mêmes fichiers sont envoyés à chaque NAM lors de la caractérisation, afin que leurs différences mesurées proviennent du modèle et non d'une performance différente.

Depuis 0.8.0, ces fichiers servent aux mesures appariées DI/rendu et aux profils
contextuels du mode texte. Ils pourront être réutilisés pour le futur mode référence
audio ; ils ne constituent pas eux-mêmes une référence de morceau.
Pour commencer rapidement, une seule prise dynamique de 20 à 30 secondes suffit.

## Livraison minimale et livraison recommandée

Une première indexation peut démarrer avec une seule guitare polyvalente et les cinq séquences décrites ci-dessous. Pour un classement plus robuste, fournissez au moins :

- une guitare à simples bobinages, positions chevalet et manche ;
- une guitare à humbuckers, positions chevalet et manche ;
- éventuellement une P90 ou une guitare active si elle fait partie de votre usage réel ;
- deux niveaux de jeu reproductibles, doux et fort, dans une même prise dynamique ou dans des fichiers séparés.

Il vaut mieux un petit jeu propre, documenté et reproductible qu'un grand dossier hétérogène.

## Format audio

| Propriété | Valeur recommandée |
|---|---|
| Conteneur | WAV non compressé |
| Encodage | PCM entier 24 bits |
| Fréquence | 48 000 Hz |
| Canaux | Mono |
| Effets | Aucun |
| Normalisation | Aucune |
| Niveau | Crêtes fortes entre environ -12 et -6 dBFS, jamais à 0 dBFS |
| Début/fin | Conserver 2 à 5 secondes de silence réel |

N'utilisez pas MP3, AAC, FLAC avec traitement préalable, WAV flottant normalisé ou fichier stéréo dupliqué. Si la station enregistre dans un autre format, conservez les originaux et exportez une copie PCM 24 bits / 48 kHz avec un convertisseur transparent, sans normalisation ni limiteur.

## Chaîne d'enregistrement

Utilisez le chemin le plus direct possible :

```text
guitare → câble → entrée instrument/Hi-Z de l'interface → enregistrement mono
```

À désactiver absolument :

- ampli ou simulation d'ampli/cabinet ;
- pédale, compresseur, gate, égaliseur, réverbération ou correction automatique ;
- normalisation, limiteur, saturation et réduction de bruit ;
- correction de phase ou alignement temporel automatique ;
- « direct monitoring » enregistré dans la piste au lieu du signal DI brut.

Le monitoring peut contenir des effets pour le confort du guitariste seulement si la piste enregistrée reste strictement sèche.

Réglez le gain d'interface une fois par guitare et micro. Jouez plus fort que prévu, puis laissez suffisamment de marge. Un écrêtage invalide la prise ; un niveau un peu faible en 24 bits est préférable. Ne modifiez pas le gain au milieu d'une série sans créer un nouvel enregistrement et le déclarer dans le manifeste.

## Contenu musical à enregistrer

Enregistrez chaque séquence au métronome, mais ne rendez pas le clic dans le WAV. Évitez les bruits de casque ou d'enceinte captés par les micros de guitare.

### 1. Notes et registre

Durée indicative : 20 à 30 secondes.

- notes isolées du grave à l'aigu ;
- plusieurs cordes et positions du manche ;
- attaques régulières, notes laissées décroître ;
- quelques notes tenues pour mesurer sustain et bruit.

### 2. Dynamique d'attaque

Durée indicative : 20 à 30 secondes.

- répéter le même motif doucement, moyennement puis fortement ;
- garder le même micro, le même volume de guitare et le même gain d'interface ;
- inclure au moins une note isolée et un accord à chaque intensité.

Cette prise est essentielle pour mesurer la compression, le seuil de saturation et la sensibilité au médiator.

### 3. Accords ouverts et complexes

Durée indicative : 20 à 30 secondes.

- accords ouverts majeurs et mineurs ;
- accords comprenant plusieurs cordes et intervalles ;
- laisser quelques accords sonner entièrement ;
- alterner arpège et attaque simultanée.

Cette prise renseigne sur la définition, l'intermodulation et le comportement dans les médiums.

### 4. Power chords et palm-mutes

Durée indicative : 20 à 30 secondes.

- power chords graves ouverts puis étouffés ;
- coups espacés et motif plus rapide ;
- attaques moyennes puis fortes ;
- au moins deux hauteurs différentes.

Cette prise est utile pour la fermeté des graves, le « chug », le relâchement et la saturation dense.

### 5. Ligne lead et articulations

Durée indicative : 20 à 30 secondes.

- phrase monodique sur plusieurs registres ;
- bends, vibrato, legato et attaques franches ;
- une note longue jusqu'à la fin naturelle ;
- jeu doux puis passage plus appuyé.

Cette prise renseigne sur le sustain, la douceur des aigus, le bruit et la perception de la dynamique.

## Guitares, micros et niveaux

Chaque combinaison réellement utilisée doit avoir son propre identifiant. Ne mélangez pas deux positions de micro dans un même WAV, sauf si la séquence a précisément pour but de documenter ce changement.

Pour chaque prise, notez :

- marque/modèle ou description stable de la guitare ;
- type de micro : `single_coil`, `humbucker`, `p90`, `active`, `piezo` ou `unknown` ;
- position : chevalet, milieu, manche ou combinaison ;
- volume et tonalité de la guitare ;
- interface et entrée utilisées ;
- gain d'entrée ou repère physique reproductible ;
- diapason/accordage si différent du standard ;
- type de cordes et médiator si connus ;
- date et remarques sur les bruits éventuels.

Un second lot avec le volume de guitare réduit peut être utile pour mesurer le « cleanup », mais il doit être identifié explicitement. Ne compensez pas sa baisse en remontant le gain de l'interface.

## Organisation des fichiers

Utilisez des noms courts, stables, sans secret personnel. Convention recommandée :

```text
di-reference/
  di-manifest.json
  sc_bridge_notes.wav
  sc_bridge_dynamics.wav
  sc_bridge_chords.wav
  sc_bridge_palmmute.wav
  sc_bridge_lead.wav
  hb_bridge_notes.wav
  hb_bridge_dynamics.wav
  hb_bridge_chords.wav
  hb_bridge_palmmute.wav
  hb_bridge_lead.wav
```

Le manifeste associe les fichiers à leurs conditions d'enregistrement. Exemple de forme d'échange :

```json
{
  "schema_version": "pipedal-ai.di-manifest/1.0.0",
  "set_id": "home-guitars-v1",
  "sample_rate_hz": 48000,
  "pcm_bits": 24,
  "channels": 1,
  "files": [
    {
      "di_id": "sc_bridge_dynamics",
      "file": "sc_bridge_dynamics.wav",
      "purpose": "dynamics",
      "guitar": "Stratocaster-type",
      "pickup_type": "single_coil",
      "pickup_position": "bridge",
      "guitar_volume": 10,
      "guitar_tone": 10,
      "input_gain_note": "interface input 1, repere 9 heures",
      "performance": "same riff soft, medium, hard",
      "notes": ""
    }
  ]
}
```

Le format exact est validé par l'outil d'ingestion du projet. N'ajoutez pas de chemins absolus : `file` doit désigner un fichier situé dans le même dossier ou un sous-dossier contrôlé. Après validation, le système calcule le SHA-256 de chaque WAV et lie les résultats à ce contenu, pas seulement au nom du fichier.

## Contrôle qualité avant envoi

Pour chaque fichier :

1. écoutez le début, le passage le plus fort et la fin au casque ;
2. confirmez qu'aucun échantillon n'est écrêté et qu'aucun traitement n'est audible ;
3. vérifiez qu'il est mono, à 48 kHz et en PCM 24 bits ;
4. vérifiez l'absence de clic de métronome enregistré, de voix ou de musique protégée en arrière-plan ;
5. conservez les silences et les fins de notes ;
6. ne coupez pas automatiquement les bruits : ils servent à mesurer le bruit réel ;
7. remplissez le manifeste et vérifiez que chaque fichier déclaré existe une seule fois.

Un contrôle technique peut être effectué avec un outil audio local tel que `ffprobe` ou `soxi`. Les fichiers qui ne correspondent pas au format attendu doivent être refusés ou convertis dans une copie ; les originaux restent inchangés.

## Procédure d'ingestion

1. Placez le dossier et son manifeste sur le **Pi**, dans un dossier d'entrée.
2. Lancez `pipedal-ai --config config/pi.toml di-import --manifest /chemin/manifest.json` : format, taille, durée, mono, fréquence, PCM, chemins et SHA-256 sont validés avant la copie gérée.
3. Corrigez les fichiers refusés ; ne forcez pas leur import. Le manifeste n'est pas réécrit.
4. Enregistrez le jeu validé comme version immuable.
5. Calculez les caractéristiques des DI, puis les empreintes d'un petit lot de NAM.
6. Comparez les résultats sur quelques modèles clean, crunch et high-gain connus avant d'indexer tout le catalogue.
7. Après la recette matérielle, le Pi pourra rendre automatiquement les mêmes DI dans PiPedal ; la RTX recevra uniquement les rendus ou leurs références autorisées pour l'analyse.

Les commandes exactes d'ingestion dépendent de la version livrée et doivent être prises dans l'aide `--help` de l'outil. Ne copiez pas un jeu de DI dans les répertoires NAM/IR de PiPedal : ce sont des données de test séparées.

## Recette matérielle avant rendu automatique

Le rendu automatique avec TooB File Player et TooB Record Input doit être testé avec le volume d'écoute abaissé et sans session live :

1. rendre une DI à travers un preset neutre et vérifier durée, canaux, niveau et absence de boucle ;
2. rendre un seul NAM connu à plusieurs niveaux d'entrée ;
3. confirmer que TooB File Player et TooB Record Input n'apparaissent jamais dans un preset live publié ;
4. surveiller charge CPU, température, espace disque et xruns ;
5. provoquer l'arrêt du service RTX et la perte réseau : PiPedal doit conserver le preset live courant ;
6. vérifier le nettoyage des fichiers temporaires après succès, échec et redémarrage ;
7. n'autoriser les lots de rendu qu'après cette recette.

## Intervention demandée maintenant

L'utilisateur doit préparer au minimum :

- une prise dynamique pour démarrer ; idéalement cinq WAV selon les séquences ;
- idéalement le même lot pour un simple bobinage chevalet et un humbucker chevalet ;
- un manifeste renseignant guitare, micro, position, réglages et gain d'interface ;
- l'autorisation d'utiliser ces prises pour les calculs locaux du projet ;
- un créneau avec accès au Pi pour le premier rendu surveillé.

Il n'est pas nécessaire de jouer parfaitement. La priorité est une prise sèche, sans écrêtage, avec des niveaux constants et des attaques variées. Ne normalisez pas les fichiers avant de les transmettre.

## Limites

- Le jeu reflète les guitares, cordes, médiators et styles enregistrés ; il doit évoluer si l'usage réel change fortement.
- Les niveaux relatifs sont aussi importants que le contenu musical. Une normalisation détruit une partie de l'information dynamique.
- Une empreinte obtenue avec une seule DI peut favoriser un type de micro ou de jeu.
- Le bruit de la prise DI et celui du NAM doivent être distingués lors de l'analyse.
- Les captures `amp` et `amp-cab` doivent être comparées dans des conditions adaptées ; une IR supplémentaire ne doit pas être appliquée aveuglément à un rig déjà complet.
- Ces WAV ne remplacent pas la validation finale dans PiPedal avec la guitare, l'interface et les niveaux réels.
