"""
Exemple de receiver webhook pour recevoir les rapports
Lance ce serveur sur ton VPS ou une machine distante
"""

from flask import Flask, request, jsonify
from datetime import datetime
import json
import os

app = Flask(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)


@app.route('/webhook/fleet', methods=['POST'])
def receive_fleet_report():
    """Reçoit et sauvegarde les rapports de création de flotte."""
    data = request.get_json()
    
    # Sauvegarder le rapport
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    partner = data.get("data", {}).get("partner", "unknown")
    
    report_file = REPORTS_DIR / f"fleet_{partner}_{timestamp}.json"
    with open(report_file, "w") as f:
        json.dump(data, f, indent=2)
    
    # Afficher résumé
    summary = data.get("data", {}).get("summary", {})
    print(f"\n📨 Rapport reçu: {partner}")
    print(f"   Total: {summary.get('total')}")
    print(f"   ✅ Succès: {summary.get('success')}")
    print(f"   ❌ Échecs: {summary.get('failed')}")
    print(f"   ⏩ Ignorés: {summary.get('skipped')}")
    print(f"   Durée: {summary.get('duration_seconds'):.1f}s")
    
    # Ici tu peux ajouter:
    # - Envoi Telegram/Discord/Email
    # - Notification push
    # - Stockage en base de données
    
    return jsonify({"status": "ok", "saved": str(report_file)})


@app.route('/reports', methods=['GET'])
def list_reports():
    """Liste tous les rapports reçus."""
    reports = sorted(REPORTS_DIR.glob("fleet_*.json"), reverse=True)
    return jsonify([r.name for r in reports[:50]])


if __name__ == '__main__':
    print(f"🚀 Webhook receiver démarré sur http://0.0.0.0:5000")
    print(f"📁 Rapports sauvegardés dans: {REPORTS_DIR}")
    app.run(host='0.0.0.0', port=5000, debug=False)
