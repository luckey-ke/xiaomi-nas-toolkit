import glob
import re
import sys

d = '/data/minas/plugins/1panel/src/ui/assets/js'
hit = 0
for f in glob.glob(d + '/*.js'):
    s = open(f, encoding='utf-8', errors='ignore').read()
    if 'panel_public_key' not in s:
        continue
    hit += 1
    m = re.search(r'let t=Jh\(.panel_public_key.\)\|\|`([^`]+)`', s)
    print('文件:', f.split('/')[-1])
    print('  嵌入公钥长度:', len(m.group(1)) if m else 0)
    m2 = re.search(r'EntranceCode=Pd\(t\.entrance\|\|`([^`]+)`\)', s)
    print('  安全入口兜底:', m2.group(1) if m2 else '未注入')
    if m:
        open('/tmp/key.txt', 'w').write(m.group(1))
print('命中文件数:', hit)
