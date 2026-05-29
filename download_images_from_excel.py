#!/usr/bin/env python3
"""
Télécharge les images depuis un Excel contenant des URLs.
Usage: python3 download_images_from_excel.py <chemin_excel> [--output <dossier>]
"""

import pandas as pd
import requests
import os
import sys
import argparse
from urllib.parse import urlparse
from pathlib import Path


def extract_filename_from_url(url: str) -> str:
    """Extrait le nom de fichier depuis l'URL."""
    parsed = urlparse(url)
    # Récupère le dernier segment du path (ex: IMG_2372.jpeg)
    filename = os.path.basename(parsed.path)
    return filename if filename else "unknown.jpg"


def download_image(url: str, output_path: Path, timeout: int = 30) -> bool:
    """Télécharge une image depuis une URL."""
    try:
        response = requests.get(url, timeout=timeout, stream=True)
        response.raise_for_status()
        
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"   ❌ Erreur téléchargement: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Télécharge les images depuis un Excel")
    parser.add_argument("excel_path", help="Chemin vers le fichier Excel")
    parser.add_argument("--output", "-o", default="./downloaded_images", 
                        help="Dossier de sortie (défaut: ./downloaded_images)")
    parser.add_argument("--url-column", default="URL image",
                        help="Nom de la colonne contenant les URLs (défaut: 'URL image')")
    
    args = parser.parse_args()
    
    # Vérifie le fichier Excel
    excel_path = Path(args.excel_path)
    if not excel_path.exists():
        print(f"❌ Fichier Excel introuvable: {excel_path}")
        sys.exit(1)
    
    # Crée le dossier de sortie
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"📁 Dossier de sortie: {output_dir.absolute()}")
    
    # Charge l'Excel
    print(f"\n📊 Chargement de {excel_path.name}...")
    df = pd.read_excel(excel_path)
    print(f"   Total lignes: {len(df)}")
    
    # Vérifie la colonne URL
    url_column = args.url_column
    if url_column not in df.columns:
        print(f"❌ Colonne '{url_column}' introuvable!")
        print(f"   Colonnes disponibles: {list(df.columns)}")
        sys.exit(1)
    
    # Compteurs
    success_count = 0
    error_count = 0
    skipped_count = 0
    
    # Traite chaque ligne
    print(f"\n⬇️  Téléchargement des images...\n")
    
    for idx, row in df.iterrows():
        url = row.get(url_column)
        
        # Skip si URL vide
        if pd.isna(url) or not str(url).strip():
            skipped_count += 1
            continue
        
        url = str(url).strip()
        filename = extract_filename_from_url(url)
        output_path = output_dir / filename
        
        # Affiche la progression
        print(f"[{idx + 1}/{len(df)}] {filename}")
        
        # Skip si déjà téléchargé
        if output_path.exists():
            print(f"   ⚡ Déjà existant, skip")
            skipped_count += 1
            continue
        
        # Télécharge
        if download_image(url, output_path):
            success_count += 1
            print(f"   ✅ Téléchargé ({os.path.getsize(output_path) / 1024:.1f} Ko)")
        else:
            error_count += 1
    
    # Résumé
    print(f"\n{'='*50}")
    print(f"📈 RÉSUMÉ:")
    print(f"   ✅ Succès: {success_count}")
    print(f"   ❌ Erreurs: {error_count}")
    print(f"   ⚡ Skips (vide/existant): {skipped_count}")
    print(f"\n📁 Images sauvegardées dans: {output_dir.absolute()}")


if __name__ == "__main__":
    main()
