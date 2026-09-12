# Tests et recette

## Correctif Windows 0.8.1

Les sous-processus d'import du catalogue utilisent UTF-8 pour l'émission et la
lecture, y compris sous Windows PowerShell 5.1. Les tests du faux serveur PiPedal
isolent le verrou système : ils s'exécutent sur Windows et Linux. Le test du vrai
verrou `fcntl` reste réservé à Linux, où le banc de rendu fonctionne. Un appel au
banc réel sur Windows est refusé explicitement avant toute création de fichier.
Les préécoutes et presets mesurés conservent aussi la même écriture de racine
que lors de leur création, y compris pour les noms courts Windows. Le contrôle
de confinement résolu et le refus des liens symboliques restent appliqués.

La CI ajoute Windows/Python 3.11 aux trois versions Python testées sous Linux.
Un test de lien symbolique peut aussi être ignoré si Windows ne permet pas sa
création. Ces exclusions sont indiquées par `skipped` et ne sont pas des échecs.

Depuis le dossier du dépôt Windows, après avoir arrêté le service RTX :

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\pipedal-ai-rtx.exe --config .\config\rtx.toml
```

Conserver `config/`, `data/` et les clés existantes. Ce correctif ne modifie ni
le modèle Ollama ni ses réglages. La suite utilise des modèles simulés : une
recette `diagnose-rtx` depuis le Pi reste nécessaire pour tester le modèle réel.

## Vérifications 0.8.0

Validation de livraison : 105 tests applicatifs et 19 tests phase zéro passés,
syntaxe JavaScript contrôlée, package Python construit. Les wheels NumPy, SciPy
et SoundFile pour Python 3.13/aarch64 ont été téléchargés pour vérifier leur
disponibilité ; cela ne constitue pas un essai d'exécution sur Raspberry Pi.

La suite couvre aussi : schéma musical fermé et corrélé, trois variantes réglées
sans paramètres inventés, types NAM contradictoires, cache de métadonnées, unités
TooB réelles, revalidation des adaptateurs sur le Pi, calibration indépendante,
DI 24 bits et chemins sûrs, hashes du transport audio, enveloppes linéaires et
saturées synthétiques, alignement et stéréo opposée, previews à crête plafonnée,
budget d'optimisation, exclusion des rendus écrêtés, cache de caractérisation,
retours par profil et routes DI authentifiées.

Le renderer est testé avec un faux serveur PiPedal : identifiants neufs pour
éviter l'emprunt d'effets live par l'API structurelle, restauration de l'état non
sauvegardé, perte de connexion, XRUN, changement utilisateur et verrou entre
processus. Les mesures synthétiques et les faux serveurs ne valident ni Ollama
réel, ni la latence, ni les chemins d'enregistrement de ton Pi. La recette du
[banc](banc-di.md) demeure la vérification matérielle à effectuer.

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
