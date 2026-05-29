import json, re
from pathlib import Path

def norm(x):
    x = (x or '').strip().upper()
    if not x or x in ('NA','N/A','NEANT','NÉANT'):
        return ''
    return re.sub(r'[^A-Z0-9]', '', x)

site_raw = [
    "4894HX01","1854GV01","9534HC01","8901HV01","1781GT01",
    "2979JK01","20HH01","2179KV01","AB-825-AF-01","AA-547-TE-01",
    "AA-136-PR-01","4520JC01","1545HL01","2399KE01","1011JX01",
    "7160FK01","AA-115-QB-01","4606ET01","2859HL01","AA-747-VB-01",
    "AA-994-YJ-01","18523WWCI01"
]
site_set = set(norm(x) for x in site_raw if norm(x))

admin = Path(r'c:\Users\c.romaric\Desktop\scraping\vps_deploy\output\admin_approved_fleet_20260423_150244.json')
d = json.loads(admin.read_text(encoding='utf-8'))
fleet = d.get('fleet', [])
print(f'Admin fleet total: {len(fleet)}')
if fleet:
    print(f'Sample keys: {list(fleet[0].keys())[:15]}')

# group by partner
from collections import defaultdict, Counter
partner_plates = defaultdict(set)
for item in fleet:
    partner = item.get('partner_name', item.get('partenaire', item.get('partner', '???')))
    plate_key = None
    for k in ['matricule','matriculation','plate','registration','immatriculation']:
        if k in item:
            plate_key = k
            break
    if not plate_key:
        for k in item:
            if 'matric' in k.lower() or 'plate' in k.lower() or 'immatric' in k.lower():
                plate_key = k
                break
    if plate_key:
        n = norm(str(item[plate_key]))
        if n:
            partner_plates[partner].add(n)

results = []
for partner, plates in partner_plates.items():
    match = site_set & plates
    if match:
        pct = len(match) / len(site_set) * 100
        results.append((partner, len(match), pct, len(plates), match))

results.sort(key=lambda x: -x[1])
print(f"\n{'Partner':<40} {'Match':>5} / {len(site_set)}  {'%':>6}  {'Fleet':>6}")
print("-" * 70)
for partner, mc, pct, fc, matched in results[:15]:
    print(f"{partner:<40} {mc:>5} / {len(site_set)}  {pct:>5.1f}%  {fc:>6}")
    if mc >= 5:
        print(f"   Matched: {sorted(matched)[:10]}...")
