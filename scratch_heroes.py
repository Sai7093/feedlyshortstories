import urllib.request
import re

url = 'https://raw.githubusercontent.com/ServiceStack/images/master/heroes.js'
with urllib.request.urlopen(url) as res:
    text = res.read().decode('utf-8')

filenames = re.findall(r'"([^"]+\.jpg)"', text)
print(f'Total filenames in heroes.js: {len(filenames)}')
for f in filenames[:10]:
    print(' ', f)
