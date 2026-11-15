"""Summarise flareattachments.ini: which flare definitions attach to which handles, and each flare's settings."""
import colorsys
import re
import sys
from collections import OrderedDict

S = OrderedDict()
sec = None
for line in open(sys.argv[1], encoding='latin-1'):
    line = line.strip()
    m = re.match(r'\[(.+)\]', line)
    if m:
        sec = m.group(1)
        S[sec] = []
        continue
    if line:
        S[sec].append(line)
print('== attachment profiles')
for s, ls in S.items():
    att = [l for l in ls if l.startswith('Attachments')]
    if att:
        pairs = []
        for a in att:
            m = re.search(r'Name="([^"]+)",\s*HandleName="([^"]+)"', a)
            if m and (m.group(1), m.group(2)) not in pairs:
                pairs.append((m.group(1), m.group(2)))
        print('[%s] ' % s + '; '.join('%s@%s' % p for p in pairs))
print('== flare definitions')
for s, ls in S.items():
    fl = [l for l in ls if l.startswith('Flares')]
    if not fl:
        continue
    out = []
    for f in fl:
        d = dict(re.findall(r'(\w+)=(\([^)]*\)|[^,()]+)', f[len('Flares=('):]))
        mat = d.get('FlareMat', '').split('.')[-1].strip("'")
        h, sat, b = d.get('DiffuseHue'), d.get('DiffuseSaturation'), d.get('DiffuseBrightness')
        rgb = ''
        if b is not None:
            # assumption: Unreal-style 0-255 HSB where saturation 255 = white (inverted), missing hue = 0,
            # missing saturation = 255; values above 255 clamped
            sv = 1.0 - min(255, int(sat if sat is not None else 255)) / 255.0
            r_, g_, b_ = colorsys.hsv_to_rgb(int(h or 0) / 255.0, sv, int(b) / 255.0)
            rgb = '#%02X%02X%02X' % (round(r_ * 255), round(g_ * 255), round(b_ * 255))
        out.append('%s I=%s HSB=%s/%s/%s %s size=%s' % (mat, d.get('Intensity'), h, sat, b, rgb,
                                                       re.sub(r',?Z=[0-9.]+', '', d.get('SizeScale', ''))))
    other = [l for l in ls if not l.startswith('Flares')]
    print('[%s] ' % s + ' | '.join(out) + '  || ' + ' '.join(other))
