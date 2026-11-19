import urllib.request
import json
import re

# Check a few known repos with mock data or wallpapers
candidate_urls = [
    # Static image lists from public sample datasets
    'https://raw.githubusercontent.com/Himanshu-Singh-Bisht/WallpaperApp/master/app/src/main/assets/wallpapers.json',
    'https://raw.githubusercontent.com/alecperkins/unsplash-json/master/index.json',
    'https://raw.githubusercontent.com/karthikg25/wallpaper-app/master/app/src/main/assets/wallpapers.json',
    'https://raw.githubusercontent.com/ghosh/uiGradients/master/gradients.json',
    'https://raw.githubusercontent.com/Biuni/PokemonGO-Pokedex/master/pokedex.json',
    'https://raw.githubusercontent.com/public-apis/public-apis/master/README.md',
    'https://raw.githubusercontent.com/bvaughn/react-virtualized/master/source/demo/sample-data.json',
    'https://raw.githubusercontent.com/react-native-elements/react-native-elements/next/example/src/data/images.ts'
]

found_ids = set()

for u in candidate_urls:
    try:
        req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=4) as res:
            text = res.read().decode('utf-8', errors='ignore')
            matches = re.findall(r'photo-([0-9a-fA-F-]+)', text)
            if matches:
                print(f'{u} -> found {len(matches)} matches!')
                found_ids.update(matches)
    except Exception as e:
        # print(f'{u} -> failed {e}')
        pass

print(f'Total unique IDs from candidates: {len(found_ids)}')
