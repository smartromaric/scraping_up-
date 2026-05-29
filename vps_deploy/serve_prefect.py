"""
Prefect — démarrage et utilisation de l’interface
================================================

Démarrage (Windows / PowerShell)
-------------------------------

Depuis le dossier ``vps_deploy``::

    python serve_prefect.py

Sans serveur Prefect séparé, Prefect 3 lance souvent un **serveur temporaire** et affiche
l’URL de l’UI (ex. http://127.0.0.1:8419). Ouvre-la dans le navigateur.

Si la commande ``prefect`` n’est pas reconnue, utilise le module Python::

    python -m prefect server start

Pour un serveur dédié sur le port 4200, configure une fois l’API (exemple)::

    python -m prefect config set PREFECT_API_URL=http://127.0.0.1:4200/api

Télémétrie (optionnel, évite des erreurs réseau inoffensives)::

    $env:PREFECT_SERVER_ANALYTICS_ENABLED = "false"


Ce que fait ce fichier
----------------------

- Enregistre tous les **déploiements** (flows exposés à l’UI).
- Démarre un **runner** : quand tu lances un run depuis l’UI, c’est ce processus qui exécute le script.


Utiliser l’interface Prefect
----------------------------

1. **Deployments** : liste des jobs. Tu y trouves :
   - ``run-vps-script`` : n’importe quel script autorisé + paramètre ``cli_args``.
   - ``sync-fleet-vps``, ``count-fleet-vps``, ``match-and-organize-vps`` : raccourcis avec champs dédiés.
   - Un déploiement par **autre** script (nom = nom du fichier sans ``.py``) : paramètre ``cli_args`` pour les flags (``--dry-run``, ``--only Partenaire1``, etc.).

2. **Lancer un run** : ouvre un déploiement → **Quick Run** ou **Run** → renseigne les paramètres → confirme.

3. **cli_args** : idéalement une **liste** d’arguments (comme après le nom du script), ex. ``["--only", "Partenaire7"]``.
   Si l’UI n’envoie qu’**une seule chaîne** du type ``["--only", "Partenaire7"]`` ou ``[--only "Partenaire1"]``, elle est
   désormais **découpée automatiquement** avant l’exécution (voir ``normalize_cli_args`` dans ``prefect_flows/flows.py``).

4. **Flow runs** : historique des exécutions ; clique un run pour voir les **logs**, l’état (réussi / échoué), annuler si besoin.

5. **Planification** : sur un déploiement, tu peux ajouter un **schedule** (cron / intervalle) pour des exécutions automatiques tant que ``serve_prefect.py`` tourne.

6. **CLI** (si ``python -m prefect`` fonctionne) ::

       python -m prefect deployment run "nom-du-flow/nom-du-deploiement"


Important
---------

- Le runner tourne sur **la machine où** ``serve_prefect.py`` est lancé (chemins Selenium / fichiers = ce PC).
- Les scripts s’exécutent avec le **répertoire de travail** ``vps_deploy``, comme un ``python mon_script.py`` local.
"""

from prefect import serve

from prefect_flows.flows import build_all_deployments

if __name__ == "__main__":
    serve(*build_all_deployments())
