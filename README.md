# PiPedal AI MVP

PiPedal AI transforme une description textuelle en trois presets (`conservative`, `balanced`, `bold`) validés et compilés sur le Raspberry Pi. Le Pi reste l'unique autorité : le PC RTX ne reçoit qu'un catalogue de capacités sans chemins locaux et ne peut ni écrire dans PiPedal ni exécuter de commandes sur le Pi.

## Ce qui est livré

- monolithe modulaire Pi : API/UI, profils guitare, SQLite, travaux, validation, compilation `.piPreset`, import et activation PiPedal ;
- service RTX : API v1 authentifiée, appel Ollama en sortie JSON structurée ;
- mode local déterministe si la RTX ou le réseau est indisponible ;
- inventaire/catalogue phase zéro complet ;
- schémas JSON versionnés, unités systemd, tests et documentation d'exploitation ;
- compatibilité structurelle testée avec les deux presets de référence fournis.

Le matching audio, la séparation de pistes et l'optimisation par rendu sont volontairement hors de ce MVP.

## Démarrage rapide

Sur le Pi, depuis ce dossier :

```bash
python3 -m venv venv
venv/bin/pip install .
cp config/pi.example.toml config/pi.toml
export PIPEDAL_AI_API_KEY='une-cle-longue-et-aleatoire'
export PIPEDAL_AI_RTX_TOKEN='un-autre-secret-long-et-aleatoire'
```

Si la base SQLite existante est déjà celle créée en phase zéro, conservez-la et indiquez son chemin dans `config/pi.toml`. Sinon :

```bash
python3 tools/phase0/pipedal_ai_inspect.py --output pipedal-inventory.json
python3 tools/phase0/pipedal_ai_catalog.py import \
  --inventory pipedal-inventory.json --database /var/lib/pipedal-ai/pipedal-ai.db
```

L'inventaire réel transmis avec le projet est conservé dans `examples/current-pi/pipedal-inventory-v2.json` pour audit et reproduction, mais il faut le régénérer si le contenu du Pi a changé.

Puis :

```bash
venv/bin/pipedal-ai-pi --config config/pi.toml
```

L'interface est sur `http://ADRESSE_DU_PI:8090`. L'import et l'activation sont désactivés par défaut ; ne les activer qu'après le test de compilation local décrit dans [Installation Pi](docs/installation-pi.md).

Sur le PC RTX :

```bash
python -m venv venv
venv/bin/pip install .
cp config/rtx.example.toml config/rtx.toml
export PIPEDAL_AI_RTX_TOKEN='le-meme-secret-rtx-que-sur-le-pi'
venv/bin/pipedal-ai-rtx --config config/rtx.toml
```

En production, utilisez le mTLS décrit dans [Sécurité](docs/security.md), et non le lancement HTTP simplifié ci-dessus.

## Documentation

- [Architecture et frontières d'autorité](docs/architecture.md)
- [Installation sur le Pi](docs/installation-pi.md)
- [Installation du service RTX](docs/installation-rtx.md)
- [Configuration](docs/configuration.md)
- [API v1](docs/api.md)
- [Format PresetSpec et `.piPreset`](docs/preset-format.md)
- [Fonctionnement du générateur local](docs/generateur-local.md)
- [Modèle SQLite et arborescence](docs/data-model.md)
- [Sécurité et modèle de menace](docs/security.md)
- [Exploitation et dépannage](docs/operations.md)
- [Tests et recette matérielle](docs/testing.md)
- [Limites et feuille de route](docs/roadmap.md)

## Tests

Les tests du cœur n'utilisent que `unittest` et Pydantic :

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
(cd tools/phase0 && python3 -m unittest discover -s tests -v)
```

Pour inclure les presets de référence :

```bash
PIPEDAL_AI_REFERENCE_PRESETS=/chemin/vers/presets \
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
