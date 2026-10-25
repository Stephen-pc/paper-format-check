import json
import pathlib
import re

pkg = json.loads(pathlib.Path('package.json').read_text(encoding='utf-8'))
yml = pathlib.Path('cordis.patch.yml').read_text(encoding='utf-8')
js = pathlib.Path('lib/index.js').read_text(encoding='utf-8')

pat = re.compile(r"name:\s*'([^']+)'")
m = pat.search(yml)
print('regex match:', m.group(1) if m else None)

yaml_name = m.group(1)
yaml_id = re.search(r"- id:\s*([\w-]+)", yml).group(1)
# lib/index.js 里 `export const name = PROVIDER_NAME`，字面量在常量上
js_name = re.search(r"const PROVIDER_NAME = '([^']+)'", js).group(1)

print('package.json name :', pkg['name'])
print('cordis.patch name :', yaml_name, '-> 一致' if yaml_name == pkg['name'] else '-> 不一致!')
print('cordis.patch id   :', yaml_id, '| lib/index.js name :', js_name,
      '-> 一致' if yaml_id == js_name else '-> 不一致!')
print('dsh.bundle.patch  :', pkg['dsh']['bundle']['patch'],
      '| 存在:', pathlib.Path(pkg['dsh']['bundle']['patch'][2:]).exists())
missing = [f for f in pkg['files'] if not pathlib.Path(f).exists()]
print('files 缺项        :', missing or '无')
