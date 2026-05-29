# scraping_up-

Dashboard web pour l’automatisation des rapports partenaires **UPJUNOO** (20 campagnes) : KPI, extraction admin, rapports HTML d’activation, planification et console temps réel.

## Démarrage rapide

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements-partner-dashboard.txt
pip install -r requirements.txt # Selenium, openpyxl, etc. (export)
copy .env.example .env          # puis renseigner les identifiants
python run_partner_dashboard.py
```

Ouvrir : **http://127.0.0.1:8765/**

## Structure

| Élément | Rôle |
|--------|------|
| `partner_dashboard/` | API FastAPI, UI statique, planificateur, exécution des jobs |
| `run_partner_dashboard.py` | Point d’entrée du serveur |
| `nightly_reports_runner.py` | Chaîne « rapports du soir » (export + HTML) |
| `export_partner_fleet_drivers_report.py` | Export admin flotte / chauffeurs |
| `generate_activation_report.py` | Rapports HTML d’activation |
| `partner_fleet_orchestrator.py` | Orchestrateur recharges / preuves |
| `output/partner_automation/` | Données locales (non versionnées) |

## Variables d’environnement

- `PARTNER_DASHBOARD_HOST` / `PARTNER_DASHBOARD_PORT` (défaut `127.0.0.1:8765`)
- `DASHBOARD_AUTO_RUN` / `DASHBOARD_AUTO_TIME` — extraction automatique
- `UPJUNOO_EMAIL` / `UPJUNOO_PASSWORD` — connexion admin (fichier `.env`)

## Planification

L’extraction auto nécessite que le serveur dashboard reste lancé à l’heure configurée (onglet **Lancer** → **Enregistrer**).

## Archives ZIP

Après les rapports HTML, le runner crée un ZIP par lot dans `output/partner_automation/zip_soir/` :

- `rapport_soir_P01_P10_*.zip` — global du lot + `P01`…`P10`
- `rapport_soir_P11_P20_*.zip` — global du lot + `P11`…`P20`

Dashboard : cocher **Créer ZIP par lot** ou **ZIP seulement** (HTML déjà présents). Téléchargement dans l’onglet **Exports HTML**.
