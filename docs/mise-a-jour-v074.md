# Correctif planificateur — v0.7.4

## Ce qui est corrigé

Le schéma de génération n'autorise plus un dictionnaire de paramètres génériques.
Chaque plugin de la liste courte possède sa branche : identifiant exact, symboles
de contrôle, types, bornes, énumérations et ressources compatibles. Par exemple,
`GxSupersonic` ne peut pas recevoir `threshold`, et `TooB Cab Simulator` ne devient
pas un chargeur d'IR. Un chargeur sans fichier compatible est exclu du schéma.

Le schéma fixe aussi l'ordre des trois variantes et la longueur maximale des chaînes.
Les contrôles déterministes restent actifs après génération, y compris avec le mode
de compatibilité `json`. Une validation JSON Schema locale supplémentaire contrôle
le contrat dynamique. La dépendance `jsonschema` doit donc être installée.

Le MVP série refuse plusieurs NAM ou plusieurs cabinets et exige un NAM explicitement
amp-only lorsqu'un cabinet lui est associé, placé après lui. Les consignes rappellent
que « bold » reste une variation du son demandé, pas un changement de genre. Cette
dernière consigne musicale n'est pas une garantie mesurée d'adhérence au prompt.

Les données de décision envoyées au modèle sont condensées : les contrôles présents
dans le schéma ne sont pas répétés dans le catalogue du prompt. Les identifiants publics,
le contrat Pi/RTX et la base SQLite ne changent pas. Aucune validation Pi n'est supprimée.

La génération utilise le mécanisme de [sorties structurées Ollama](https://docs.ollama.com/capabilities/structured-outputs).
Les tests locaux vérifient le schéma et simulent les échanges ; ils ne remplacent pas
une génération sur votre installation Ollama ni l'import et l'écoute sur PiPedal.
Les traces privées ne sont pas ajoutées au dépôt.

## Installation : conserver configuration, clés et données

Arrêter uniquement les serveurs **AI** avec `Ctrl+C`, pas PiPedal et son audio.
Exécuter les commandes dans les terminaux ayant déjà les variables de clés chargées.
Si `git pull --ff-only` échoue, arrêter et examiner le message : pas de reset nécessaire.

Windows, PowerShell :

```powershell
cd C:\Users\Emilien\Desktop\IA\PipedalAI\PipedalAI
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -c "import pipedal_ai; print(pipedal_ai.__version__)"
.\.venv\Scripts\pipedal-ai-rtx.exe --config .\config\rtx.toml
```

La version doit être `0.7.4`. Garder le modèle et `output_format = "schema"` dans la
configuration actuelle ; ne pas régénérer les clés.

Raspberry Pi :

```bash
cd ~/PipedalAI/PiPedal_AI_MVP_v0.5.0
git pull --ff-only origin main
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/pipedal-ai --config config/pi.toml diagnose-rtx \
  --prompt "Son blues chaud, léger crunch dynamique, aigus doux, graves fermes" \
  --output data/rtx-check-v074
```

Le diagnostic appelle Windows sans repli local, valide et compile trois presets sans
les importer. Le serveur AI Pi n'a pas besoin d'être démarré pour ce diagnostic.
Après succès, relancer `.venv/bin/pipedal-ai-pi --config config/pi.toml`, puis essayer
l'import d'un nouveau preset et l'écoute à niveau prudent.

Si la génération échoue, conserver l'erreur complète et les nouvelles traces Windows
de `data/ollama-diagnostics/`. Ne pas passer automatiquement à `format = "json"`, ni
supprimer les paramètres refusés : cela masquerait le défaut. Un succès de diagnostic
ne constitue pas encore une validation de la qualité sonore.
