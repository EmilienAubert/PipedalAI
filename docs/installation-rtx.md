# Installation sur le PC RTX

Le service fonctionne sous Linux natif, WSL2 ou Windows avec Python 3.11+. Le fichier systemd fourni cible Linux ; sous Windows, lancez la commande via un compte de service local.

1. Installez Ollama et chargez le modèle choisi dans `config/rtx.toml`.
2. Installez PiPedal AI dans un environnement virtuel.
3. Placez le secret partagé dans `PIPEDAL_AI_RTX_TOKEN`.
4. En production, placez les certificats mTLS puis lancez :

```bash
venv/bin/pipedal-ai-rtx --config /etc/pipedal-ai/rtx.toml \
  --ssl-certfile /etc/pipedal-ai/tls/rtx.crt \
  --ssl-keyfile /etc/pipedal-ai/tls/rtx.key \
  --ssl-ca-certs /etc/pipedal-ai/tls/ca.crt
```

Le pare-feu du PC doit autoriser le port RTX uniquement depuis l'adresse du Pi. Ollama doit rester lié à `127.0.0.1` : il n'a aucune raison d'être exposé au LAN.

La RTX ne stocke pas de chemin Pi, ne télécharge pas d'asset et n'appelle jamais PiPedal.
Elle extrait un `ToneIntent`, construit une liste courte à partir des identifiants reçus et
retourne uniquement un `ProposalSet` JSON. La commande locale
`pipedal-ai-fingerprint --help` prépare un index de rendus WAV sans ouvrir d'API de fichiers.
