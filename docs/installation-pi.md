# Installation sur le Raspberry Pi

## Prérequis

- Raspberry Pi OS 64 bits, Python 3.11 ou plus récent ;
- PiPedal installé et fonctionnel ;
- `lv2info` disponible ;
- accès en lecture à `/var/pipedal/audio_uploads` et aux IR d'usine TooB ;
- utilisateur de service dédié en production.

## Installation applicative

```bash
cd ~/PipedalAI/pipedal-ai-mvp
python3 -m venv venv
venv/bin/pip install .
sudo install -d -o "$USER" -g "$USER" -m 700 /var/lib/pipedal-ai
cp config/pi.example.toml config/pi.toml
```

L'exemple écoute uniquement sur `127.0.0.1`. Adaptez `server.host`, limitez
`allowed_cidrs` aux clients réellement utiles, puis configurez `database`, `artifact_root`,
l'adresse RTX et les certificats dans `config/pi.toml`. Le service refuse désormais une
écoute hors loopback si `PIPEDAL_AI_API_KEY` est vide.

## Catalogue initial

La base que vous avez déjà publiée en phase zéro peut être réutilisée telle quelle. Pour repartir d'un inventaire actuel :

```bash
python3 tools/phase0/pipedal_ai_inspect.py --output pipedal-inventory.json
python3 tools/phase0/pipedal_ai_catalog.py import \
  --inventory pipedal-inventory.json \
  --database /var/lib/pipedal-ai/pipedal-ai.db
```

Le collecteur ignore les liens externes sauf les cibles d'usine TooB explicitement approuvées. Relancez inventaire + import après ajout ou retrait de plugins, NAM ou IR. Une nouvelle empreinte crée une révision immuable.

## Premier lancement sans écriture PiPedal

Laissez `allow_import=false` et `allow_activation=false` :

```bash
export PIPEDAL_AI_API_KEY="$(openssl rand -hex 32)"
export PIPEDAL_AI_RTX_TOKEN="$(openssl rand -hex 32)"
venv/bin/pipedal-ai-pi --config config/pi.toml
```

Générez un travail depuis l'interface. Le mode local produit trois archives même si la RTX est absente. Vérifiez-en une :

```bash
venv/bin/pipedal-ai --config config/pi.toml verify-preset /var/lib/pipedal-ai/artifacts/JOB/fichier.piPreset
```

Importez d'abord manuellement cette archive via l'interface PiPedal. Une fois le test réussi, passez `allow_import=true`. N'activez `allow_activation=true` qu'après avoir vérifié le preset à faible volume.

## Service systemd

Installez l'application dans `/opt/pipedal-ai`, copiez `deploy/systemd/pipedal-ai-pi.service`, la configuration et un fichier `/etc/pipedal-ai/pi.env` lisible uniquement par root. Le service fourni applique un système de fichiers en lecture seule, à l'exception de `/var/lib/pipedal-ai`.

Adaptez `User`, `Group` et les chemins à votre installation, puis :

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pipedal-ai-pi
sudo systemctl status pipedal-ai-pi
```
