import json
p = r'c:\Users\c.romaric\Desktop\scraping\vps_deploy\output\organized_by_partner\partenaire5\data.json'
with open(p, 'r', encoding='utf-8') as f:
    d = json.load(f)

def clean(o):
    if isinstance(o, list):
        for x in o: clean(x)
    elif isinstance(o, dict):
        if 'assignment_status' in o:
            del o['assignment_status']
        for v in o.values():
            clean(v)

clean(d)
with open(p, 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
