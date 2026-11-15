import json, os, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from lead import LeadArchive, read_index, parse_fields, K, num
data = Path(sys.argv[1])
c = json.loads((data / 'catalogs.json').read_text('utf-8'))
cnt = collections.Counter(); seen = set()
for e in c:
    for m in e.get('materials') or []:
        if m['hash'] in seen: continue
        seen.add(m['hash'])
        v = m['data'].get('BD7D8F02')
        if v and v['x'][:8] == 'CA9D0FD8': cnt[m['cls']] += 1
print('materials using D80F9DCA as Diff Map by class:', dict(cnt))
p = Path(os.environ['UMDS']) / 'Blacklist.ass'
idx = read_index(p)
t = [x for x in idx[K['textures_2d']] if num(x[K['name']]) == 0xD80F9DCA][0]
m = idx[K['texture_mips']][t[K['m_index']]]
a = LeadArchive(p); a.position = m[K['POS']]; f = parse_fields(a.read(m[K['size']])); a.close()
h = f[K['handle']]
print('unique BGRA texels in top mip:', collections.Counter(h[i:i+4].hex() for i in range(0, 256, 4)).most_common(3))
