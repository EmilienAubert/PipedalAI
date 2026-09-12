# Mise à jour 0.8.0 — Pi et Windows existants

Conserve `data/`, les configurations, les secrets et les modèles Ollama. Aucun
reset Git, nettoyage de données, nouvelle installation d'Ollama ou nouveau venv
n'est nécessaire. Les changements portent sur PiPedal AI des deux machines.
La migration SQLite vers `user_version=5` est automatique, sans suppression des
catalogues, profils, travaux ni presets. Sauvegarde la base avant une migration ;
arrête le service puis copie aussi les éventuels fichiers `-wal` et `-shm`.

## 1. Windows, serveur RTX

Dans le terminal qui lance PiPedal AI RTX, arrête-le avec Ctrl+C. Ollama reste lancé.

```powershell
cd C:\Users\Emilien\Desktop\IA\PipedalAI\PipedalAI
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -c "import pipedal_ai, pipedal_ai.rtx.ollama as m; print(pipedal_ai.__version__); print(m.__file__)"
```

Attendu : `0.8.0` et un chemin vers `src\pipedal_ai\rtx\ollama.py`.
L'installation ajoute NumPy, SciPy et SoundFile pour les mesures. Si Git refuse
le pull, lis son message : ne force pas un reset qui supprimerait du travail.

Dans ton `config/rtx.toml`, conserve `[server]`, `[fingerprints]` et ton modèle.
La section `[ollama]` doit contenir :

```toml
planning_mode = "musical"
base_url = "http://127.0.0.1:11434"
model = "orcarouter/Qwen3.8-27B-Uncensored:iq3_m"
output_format = "schema"
think = false
num_ctx = 16384
num_predict = 4096
timeout_seconds = 240
temperature = 0.0
max_retries = 1
max_plugin_candidates = 24
max_assets_per_role = 12
diagnostics_directory = "./data/ollama-diagnostics"
```

La nouvelle sortie contient une seule chaîne et au plus trois candidats NAM,
au lieu de trois dictionnaires LV2 complets. Tu peux garder `num_predict=8192`
si c'est ta valeur actuelle ; il n'est plus nécessaire de l'augmenter pour le plan.
Le diagnostic distingue un contexte saturé d'une limite de sortie atteinte.

Recharge le **même** token RTX dans ce terminal si nécessaire, puis démarre :

```powershell
$env:PIPEDAL_AI_RTX_TOKEN = 'TON_TOKEN_RTX_EXISTANT'
.\.venv\Scripts\pipedal-ai-rtx.exe --config .\config\rtx.toml
```

Le token doit rester identique à celui du Pi. Ne colle jamais sa valeur dans un
rapport ou un commit. Le pare-feu déjà configuré sur le port 8091 reste valable.

## 2. Pi, autorité

Arrête PiPedal AI avec Ctrl+C ; garde PiPedal en fonctionnement.

```bash
cd ~/PipedalAI/PiPedal_AI_MVP_v0.5.0
git pull --ff-only
.venv/bin/python -m pip install -e .
.venv/bin/python -c 'import pipedal_ai; print(pipedal_ai.__version__)'
```

Si SoundFile signale que `libsndfile` est absent :

```bash
sudo apt install libsndfile1
```

Recharge les clés existantes **dans ce terminal** :

```bash
export PIPEDAL_AI_API_KEY='TA_CLE_PI_EXISTANTE'
export PIPEDAL_AI_RTX_TOKEN='TON_TOKEN_RTX_EXISTANT'
```

Conserve le catalogue et les chemins de ton `config/pi.toml`. Vérifie seulement
`[rtx] base_url="http://192.168.1.17:8091"`, `enabled=true` et un délai assez long
(par exemple `timeout_seconds=1200`). PiPedal reste sur le port 80. Utilise HTTPS
uniquement si tu as réellement lancé RTX avec certificat TLS.

Premier contrôle, sans repli local ni import :

```bash
.venv/bin/pipedal-ai --config config/pi.toml diagnose-rtx \
  --prompt "Lead rock instrumental, saturation ferme, médiums présents, aigus doux, léger delay, sans fuzz" \
  --output data/rtx-check-v080
```

Le dossier contient trois `.piPreset` et `proposals.json` avec les choix et les
avertissements. Avec le banc encore désactivé, démarre l'interface :

```bash
.venv/bin/pipedal-ai-pi --config config/pi.toml
```

Recharge la page avec Ctrl+F5 pour charger `bench.js`. Génère un **nouveau** travail.
Vérifie sa source `rtx`, consulte « Choix musicaux », importe puis écoute les trois
variantes. Les anciens travaux conservent leurs anciens réglages ; le code ne les
réécrit pas. Une calibration NAM facultative peut être renseignée dans un nouveau
profil : c'est une mesure technique, distincte du trim et de la saturation demandée.

## 3. Ta prochaine intervention

Une DI mono de 20 à 30 secondes suffit pour démarrer : même motif avec attaques
douces, moyennes et fortes, notes tenues, accords et quelques palm-mutes. Sans
effet ni normalisation, 48 kHz/24 bits, crêtes idéalement −12 à −6 dBFS.
Note guitare, micro, position, volume, tonalité et gain d'interface.

Importe-la dans le panneau du banc ou via [le manifeste DI](jeu-di-reference.md).
Puis suis [le guide du banc](banc-di.md) pour l'activer explicitement hors live.
La recette physique du File Player/Record Input, les niveaux et la restauration
doivent être vérifiés sur ton Pi avant de lancer une longue caractérisation.
