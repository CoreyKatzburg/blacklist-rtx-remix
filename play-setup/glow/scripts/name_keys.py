"""Name hashed reflection keys: hash every printable ASCII string in the game executables
(and the reader's string literals) and match against keys seen in catalogs.json / indexes.json."""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from leadhash import lead_hash  # noqa: E402

data = Path(sys.argv[1])
exes = sys.argv[2:]

keys = Counter()
cats = json.loads((data / 'catalogs.json').read_text('utf-8'))


def walk(d):
    for k, v in d.items():
        keys[k] += 1
        if isinstance(v, dict) and v.get('t') == 'f':
            walk(v['v'])


for c in cats:
    for m in c.get('materials') or []:
        walk(m['data'])
idx = json.loads((data / 'indexes.json').read_text('utf-8'))
for a in idx:
    for arr in ('textures_2d', 'texture_mips', 'atlases_2d'):
        for r in a.get(arr) or []:
            for k in r:
                keys[k] += 0

cands = set()
for exe in exes:
    blob = Path(exe).read_bytes()
    for m in re.finditer(rb'[\x20-\x7e]{2,80}', blob):
        cands.add(m.group().decode('ascii'))
    for m in re.finditer(rb'(?:[\x20-\x7e]\x00){2,80}', blob):
        cands.add(m.group().decode('utf-16le'))
# also split long strings into words
extra = set()
for s in cands:
    for w in re.split(r'[^A-Za-z0-9_ ]+', s):
        if 2 <= len(w) <= 60:
            extra.add(w.strip())
cands |= extra
table = {}
for s in cands:
    try:
        table.setdefault('%08X' % lead_hash(s), s)
    except UnicodeEncodeError:
        pass
named = {k: table.get(k) for k in keys}
out = {k: {'name': named[k], 'count': keys[k]} for k in sorted(keys, key=lambda k: -keys[k])}
(data / 'key_names.json').write_text(json.dumps(out, indent=1), 'utf-8')
print('keys', len(keys), 'named', sum(1 for v in named.values() if v))
for k in sorted(keys, key=lambda k: -keys[k])[:150]:
    print(k, keys[k], named[k])
