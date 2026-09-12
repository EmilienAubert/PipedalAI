# Mise à jour propre Pi/Windows et diagnostic RTX — v0.7.3

Cette version repart du dépôt, sans reprendre les conversions permissives des correctifs
expérimentaux. La compatibilité musicale et matérielle reste à vérifier sur vos machines.

## Ce qui change

Ollama extrait un `ToneIntent`, puis génère un `PlanDraft`. Le service RTX vérifie le travail
et le catalogue reçus avant de construire le `ProposalSet` public. Le Pi garde toutes ses
validations et compile les presets. La RTX ne touche jamais directement PiPedal.

Le schéma envoyé au modèle est dérivé de Pydantic et également placé dans le prompt.
Les wrappers d'outils et résumés de catalogue sont refusés, pas convertis silencieusement.
Une erreur HTTP 400 conserve le message Ollama exact ; elle ne retire jamais automatiquement
le schéma. Une nouvelle tentative reçoit la réponse rejetée et ses erreurs.

Les travaux exposent `fallback_reason` dans l'API et l'interface. Un succès local garde
`source: "degraded"`, même avec `error: null`. SQLite migre automatiquement vers la version 4,
sans refaire l'inventaire ni supprimer les profils, travaux, artefacts ou métadonnées.

## 1. Arrêter uniquement les serveurs AI

Dans leurs terminaux : `Ctrl+C`. Ne pas arrêter PiPedal : l'audio live est indépendant.
Si le serveur AI Pi utilise systemd, arrêter uniquement `pipedal-ai-pi`.

## 2. Mettre à jour le Raspberry Pi

```bash
cd ~/PipedalAI/PiPedal_AI_MVP_v0.5.0

# Sauvegarde cohérente de SQLite, chemin lu dans votre configuration.
.venv/bin/python -c 'import sqlite3,tomllib; from pathlib import Path; c=tomllib.loads(Path("config/pi.toml").read_text()); p=c.get("storage",{}).get("database","./data/pipedal-ai.db"); src=sqlite3.connect(p); dst=sqlite3.connect(p+".pre-v073.bak"); src.backup(dst); dst.close(); src.close(); print("Sauvegarde SQLite terminée")'

git stash push -u -m "Sauvegarde locale avant v0.7.3"
git pull --ff-only origin main
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Si `pull --ff-only` échoue, arrêter ici : des commits locaux peuvent diverger.
Ne pas contourner avec `reset --hard` ou `git clean`.

`data/`, `.venv/`, `config/pi.toml` et `config/rtx.toml` sont ignorés par Git et restent
sur les machines. Le stash conserve le code modifié et les nouveaux fichiers non ignorés.
Ne pas faire `stash pop` : cela réintroduirait les correctifs expérimentaux.
Le nom historique du dossier n'a pas besoin de changer.

Dans la section `[rtx]` existante de `config/pi.toml`, vérifier :

```toml
enabled = true
base_url = "http://192.168.1.17:8091" # Adapter à l'IP Windows réelle.
timeout_seconds = 600
bearer_token_env = "PIPEDAL_AI_RTX_TOKEN"
```

Le délai Pi englobe deux étapes et leurs reprises : prévoir plus que
`2 * (max_retries + 1) * ollama.timeout_seconds`, avec une marge.
Le protocole doit correspondre au serveur réellement lancé. HTTP ne chiffre pas le token :
limiter le diagnostic au LAN filtré et maîtrisé. Pour la production, utiliser le mTLS décrit
dans [Sécurité](security.md), sans désactiver la vérification des certificats.

## 3. Mettre à jour Windows RTX — PowerShell

```powershell
cd C:\Users\Emilien\Desktop\IA\PipedalAI\PipedalAI
git stash push -u -m "Sauvegarde locale avant v0.7.3"
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -c "import pipedal_ai; import pipedal_ai.rtx.ollama as m; print(pipedal_ai.__version__); print(m.__file__)"
```

La dernière commande doit afficher `0.7.3` et le module sous `src\pipedal_ai\rtx\ollama.py`.
Un chemin `site-packages` est normal avec une installation non editable, mais indique ici
que la nouvelle installation editable n'a pas été prise en compte. Ne pas éditer les modules
directement sous `site-packages`.

Dans la section `[ollama]` existante de `config/rtx.toml` :

```toml
base_url = "http://127.0.0.1:11434"
model = "orcarouter/Qwen3.8-27B-Uncensored:iq3_m"
timeout_seconds = 120
temperature = 0.0
max_retries = 1
output_format = "schema"
think = false
num_ctx = 8192
num_predict = 4096
diagnostics_directory = "./data/ollama-diagnostics"
```

Ne pas créer une seconde section `[ollama]`. Le modèle doit être présent dans `ollama list`.
Pour GPT-OSS, utiliser `think = "low"`. `think = "auto"` omet l'option et laisse Ollama décider.
`output_format = "json"` est un choix de compatibilité explicite, uniquement après lecture
de l'erreur exacte ; le schéma reste dans le prompt et la validation reste stricte, mais
la sortie peut être moins fiable. Aucun nouveau modèle ne doit être téléchargé pour commencer.

Sources : [sorties structurées Ollama](https://docs.ollama.com/capabilities/structured-outputs)
et [réflexion Ollama](https://docs.ollama.com/capabilities/thinking).

## 4. Charger les secrets et redémarrer

Ne pas régénérer les clés existantes. Le même `PIPEDAL_AI_RTX_TOKEN` doit être chargé dans
les terminaux Windows et Pi. `PIPEDAL_AI_API_KEY` est indépendante et sert à l'interface du Pi.
Un nouveau terminal peut ne pas avoir ces variables : charger vos secrets habituels.
Ne jamais mettre les valeurs dans Git ou une capture partagée.

Windows :

```powershell
.\.venv\Scripts\pipedal-ai-rtx.exe --config .\config\rtx.toml
```

Pi :

```bash
.venv/bin/pipedal-ai-pi --config config/pi.toml
```

Recharger l'interface avec `Ctrl+F5`. Un voyant réseau positif ne garantit pas que la
génération IA est conforme : regarder la provenance du travail.

## 5. Faire le test réel sans mode dégradé

Depuis un autre terminal sur le Pi, avec les secrets nécessaires chargés :

```bash
cd ~/PipedalAI/PiPedal_AI_MVP_v0.5.0
.venv/bin/pipedal-ai --config config/pi.toml diagnose-rtx \
  --prompt "Son blues chaud, léger crunch dynamique, aigus doux, graves fermes" \
  --output data/rtx-check-v073
```

Cette commande utilise la vraie RTX et le catalogue actif du Pi, sans repli local.
Elle vérifie la corrélation, valide trois propositions et compile trois `.piPreset`.
Elle ne les importe ni ne les active. Le succès affiche `source: "rtx"`, les trois variantes
et leurs chemins. L'échec sort avec un code non nul et un diagnostic explicite.

Après succès, importer un nouveau fichier avec « Upload preset » dans PiPedal. L'import
réel et l'écoute sont encore nécessaires. Puis tester l'interface AI et l'import automatique,
sans activer automatiquement une chaîne non écoutée.

## 6. Si cela échoue

- Lire `fallback_reason` dans l'interface ou l'API : il conserve le détail RTX.
- `étape=intent` ou `étape=plan` indique la partie en échec.
- Réponse vide : vérifier `message.content`, `message.thinking` et `done_reason`.
  Le raisonnement n'est jamais interprété comme une réponse métier.
- `done_reason: "length"` signifie une sortie tronquée. Ajuster `num_predict` et les délais.
- HTTP 400 : corriger l'erreur réellement renvoyée, sans supposer une incompatibilité du modèle.
- HTTP 401 : vérifier le secret chargé et son égalité sur les machines, pas les plugins.
- Les traces Windows sous `data/ollama-diagnostics/` contiennent requête et réponse brute,
  succès ou échec. Au maximum 32 traces bornées sont conservées. Aucun en-tête d'authentification
  n'y est enregistré, mais elles peuvent contenir le prompt, le profil guitare et le catalogue.
  Les garder privées et vérifier avant partage. Retirer `diagnostics_directory` les désactive.

Les tests automatisés simulent Ollama. Ils ne remplacent pas cette recette réelle ni les
mesures de latence, niveaux audio et réserve CPU sur votre Pi.
