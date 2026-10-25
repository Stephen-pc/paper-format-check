#!/usr/bin/env python3
"""paper-format-check —— 论文 / 实验报告格式体检（入口）。

只读：绝不修改原件。只查格式参数，不做内容审查。

三条判定原则（见设计稿 §8.3）：
  1. 读不到就不报 —— 属性缺失时报 info，不报 error；
  2. 不确定就降级 —— 文档网格、固定值行距、自动编号、SEQ 域，一律降级；
  3. 能定位才报 —— 每条 finding 必须指到「标题路径 + 段内序号 + 摘录」。

一条硬规矩（§7.6）：**本次真正比较过的检查项为 0 时，禁止判通过**。
注意口径是「真正比较过」，不是「函数被调用过」——全 null 的预设会让每个检查器都被调用
却一次比较都不做，那是比空预设更容易被写出来的形态。

退出码：0 通过，1 有问题（或 0 项检查，或 --strict 下有 warn），2 文件读不了。

分层：本文件是编排层（规则加载 / 聚合 / 报告 / CLI），
      解析在 ooxml.py，判定在 checks.py。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from ooxml import (count_body_images, iter_sections, parse_styles, q, walk_paragraphs)
from checks import (
    BODY_KEYS,
    CHECK_ORDER,
    Report,
    check_body,
    check_breaks,
    check_captions,
    check_header,
    check_headings,
    check_margins,
    has_any,
    is_heading,
    locator_of,
    resolve_body_range,
)

def die(msg: str, code: int = 2):
    """以指定退出码退出。`raise SystemExit("文本")` 实际返回 1，CI 分不清
    「文件坏了」和「格式不合格」，所以显式给码。"""
    print(f'paper-format-check: {msg}', file=sys.stderr)
    exc = SystemExit(code)
    exc.msg = msg          # 批量模式要显示「为什么读不了」，不能只有一句「读不了」
    raise exc



# ---------------------------------------------------------------- 主流程


def read_json(path: Path) -> dict:
    """utf-8-sig：Windows 记事本与 PowerShell 5 的 Set-Content 默认写 BOM，
    学生手抄一份规则文件大概率踩中。"""
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        die(f'找不到规则文件：{path}')
    except OSError as exc:
        die(f'读不了规则文件：{path}（{exc}）')
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        die(f'规则文件不是合法 JSON：{exc}')



def load_rules(preset: str, rules_path: str | None) -> tuple[dict, str, dict]:
    """规则文件只有一种形态：{version, defaults, presets}。

    --rules 换的是「规则文件」，不是「预设」—— 换完之后仍要用 --preset 选。
    传裸预设直接拒绝：v0.1 把它当单预设返回，导致所有检查静默跳过并输出「通过」。
    """
    if rules_path:
        data = read_json(Path(rules_path))
    else:
        data = read_json(Path(__file__).resolve().parent.parent / 'references' / 'rules.json')

    if not isinstance(data, dict) or 'presets' not in data:
        die('规则文件的形态不对。需要 {\n'
            '  "version": 1,\n'
            '  "defaults": {},\n'
            '  "presets": { "我的格式": { "body": { ... } } }\n'
            '}\n然后用 --preset 我的格式 选中它。')

    presets = data.get('presets') or {}
    if preset not in presets:
        die(f'没有预设 {preset!r}。可用：' + ('、'.join(presets.keys()) or '（空）'))

    merged = dict(data.get('defaults') or {})
    merged.update(presets[preset])
    return merged, preset, data



def apply_ignore(rules: dict, spec: str) -> tuple[dict, list[str]]:
    """`--ignore` 的一次性豁免：把指定检查项从规则里去掉，等价于该次不查。

    为什么需要它：一个学院的页边距就是 2.5/2.0 时，工具会对它的学生永远给 ❌，
    而出口只有「改 JSON」（§3.1 说了这类用户不会读源码）或无视。豁免给了第三条路：
    不碰规则文件，本次不查，且输出里写明忽略了什么。
    """
    import copy as _copy
    out = _copy.deepcopy(rules)
    ignored: list[str] = []
    for item in (x.strip() for x in spec.split(',') if x.strip()):
        if '.' in item:
            sec, key = item.split('.', 1)
            section = out.get(sec)
            if isinstance(section, dict) and key in section:
                section[key] = None
                ignored.append(item)
        elif item in out:
            out.pop(item)
            ignored.append(item)
    return out, ignored



def run(path: Path, rules: dict, preset: str, sid: str, sname: str,
        strict: bool = False, no_group: bool = False) -> dict:
    if not path.exists():
        die(f'找不到文件：{path}')
    if path.suffix.lower() != '.docx':
        die(f'只认 .docx，收到的是 {path.suffix or "无扩展名"}。请先用 Word/WPS 另存为 .docx。')

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        die(f'打不开这个文件（不是有效的 docx，或已加密）：{exc}')

    with zf as z:
        names = z.namelist()
        if 'word/document.xml' not in names:
            die('这不是一个有效的 .docx（缺 word/document.xml）')

        styles, defaults = parse_styles(z)
        try:
            root = ET.fromstring(z.read('word/document.xml'))
        except ET.ParseError as exc:
            # 半下载、WPS 崩溃写坏的 docx 都是这个形态。必须报成「文件坏了」（2），
            # 不能让它冒成 traceback 或被 CI 当成「格式不合格」（1）。
            die(f'文件已损坏，XML 解析失败（{exc}）。请用 Word/WPS 打开后另存为新的 .docx。')
        body = root.find(q('body'))
        paras = walk_paragraphs(body if body is not None else root, styles, defaults,
                                {'in_table': False, 'in_textbox': False, 'in_deleted': False}, [0])

        image_count = count_body_images(root)
        table_count = len(list(root.iter(q('tbl'))))
        grid_el = root.find(f'.//{q("docGrid")}')
        grid_pitch = int(grid_el.get(q('linePitch'))) if grid_el is not None and grid_el.get(q('linePitch')) else None

        headings_by_index: dict[int, str] = {}
        current = ''
        for p in paras:
            if p.text and not p.in_table and not p.in_textbox and not p.in_deleted and is_heading(p.props, p.text):
                current = p.text[:20]
            headings_by_index[p.index] = current

        def heading_of(idx: int) -> str:
            return headings_by_index.get(idx, '')

        report = Report()

        # 顺序即优先序：前三条是「不改就退」的
        check_header(z, rules, report, sid, sname)
        check_margins(root, rules, report)

        body_paras = paras
        if has_any(rules.get('body') or {}, BODY_KEYS):
            body_paras, range_note = resolve_body_range(paras, rules)
            if range_note:
                report.add('bodyRange', 'info', range_note,
                           '在预设里配置 bodyRange.startAfter / endAt，可把封面、目录、参考文献排除在外')
            else:
                report.ran('bodyRange')

        check_body(body_paras, rules, report, grid_pitch, heading_of)
        check_breaks(body_paras, rules, report, heading_of)
        check_captions(paras, rules, report, heading_of, image_count, table_count)
        check_headings(paras, rules, report)

    order = {k: i for i, k in enumerate(CHECK_ORDER)}
    raw = report.findings
    raw.sort(key=lambda f: (order.get(f['kind'], 99), f['paragraph'] or 0))

    summary = {'error': 0, 'warn': 0, 'info': 0}
    for f in raw:
        summary[f['severity']] = summary.get(f['severity'], 0) + 1

    findings = raw if no_group else _group_findings(raw)
    findings = [{'id': f'F{i:03d}', **f} for i, f in enumerate(findings, start=1)]

    if not report.executed:
        passed = False
        verdict = '没有可执行的检查项，请检查规则文件或预设名'
    elif summary['error'] > 0:
        passed = False
        verdict = f'有 {summary["error"]} 处必须修改'
    elif summary['warn'] > 0 and strict:
        passed = False
        verdict = f'{summary["warn"]} 处建议核对，--strict 下视为不通过'
    elif summary['warn'] > 0:
        passed = True
        verdict = f'可以通过，另有 {summary["warn"]} 处建议核对'
    else:
        passed = True
        verdict = '全部通过'

    return {
        'file': path.name,
        'preset': preset,
        'generatedAt': datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
        'stats': {
            'paragraphs': sum(1 for p in paras if p.text and p.index is not None),
            'tables': table_count,
            'images': image_count,
            'sections': len(iter_sections(root)),
            'checked': report.executed,
            'occurrences': len(raw),
        },
        'findings': findings,
        'summary': summary,
        'passed': passed,
        'verdict': verdict,
        'strict': strict,
        'truncated': 0,
    }



SEVERITY_ORDER = {'error': 0, 'warn': 1, 'info': 2}



def _group_findings(raw: list[dict]) -> list[dict]:
    """把「同一类问题、同一组实际值/期望值」合并成一条，带出现次数与最多 3 个示例。

    为什么必须聚合：真实报告里同一处规则不符会在几十个段落上重复出现
    （字体、行距、标题字号），逐条列出会得到 500+ 行 —— 学生看一眼就关掉，
    等于没检查。summary 仍然统计**出现次数**，不统计组数，避免把问题数量藏起来。
    """
    groups: dict[tuple, dict] = {}
    for f in raw:
        # style_only 进键：来自样式的问题改样式一处即可（可以合并），
        # 段落直接格式的问题必须逐段改（不能合并，否则「改完 3 处还剩 2 处」）。
        key = (f['kind'], f['severity'], f['problem'], f['fix'], f['style_only'])
        g = groups.get(key)
        if g is None:
            g = {**f, 'count': 0, 'samples': []}
            groups[key] = g
        g['count'] += 1
        # samples 收**全部**定位，不在聚合层截断 —— 截断由渲染层做。
        # 位置丢失等于违背「能定位才报」，一条 finding 说 count=5 却只列 3 个位置，
        # 学生改完复跑还是 2 条 error。
        if f['locator'] and f['locator'] not in g['samples']:
            g['samples'].append(f['locator'])
    for g in groups.values():
        if g['style_only'] and g['style_name'] and g['count'] > 1:
            g['fix'] = f"{g['fix']}（该段格式来自样式「{g['style_name']}」，改样式一处即可全部生效）"
    return list(groups.values())



def collect_docx(root: Path) -> list[Path]:
    """目录下所有 .docx，递归；跳过 Word 的 ~$ 锁文件（162 字节，不是文档）。"""
    if root.is_file():
        return [root]
    out = [p for p in sorted(root.rglob('*.docx')) if not p.name.startswith('~$')]
    return out



def run_batch(root: Path, rules: dict, preset: str, sid: str, sname: str,
              strict: bool, no_group: bool) -> dict:
    """批量检查。每份只保留结论，不展开 findings —— 助教要的是「哪几份要退」。

    读不了的文件单独归类：它既不是「收」也不是「格式不合格」，混进去会让
    「要退几份」这个数失真。
    """
    files = collect_docx(root)
    results: list[dict] = []
    for f in files:
        try:
            r = run(f, rules, preset, sid, sname, strict, no_group)
        except SystemExit as exc:
            # die() 抛的：文件不存在/不是 docx/损坏。stderr 已经打过原因。
            results.append({
                'file': f.name, 'path': str(f), 'status': 'unreadable',
                'exit': exc.code if isinstance(exc.code, int) else 2,
                'summary': None, 'verdict': getattr(exc, 'msg', '读不了'),
            })
            continue
        results.append({
            'file': f.name, 'path': str(f),
            'status': 'pass' if r['passed'] else 'fail',
            'exit': 0 if r['passed'] else 1,
            'summary': r['summary'], 'verdict': r['verdict'],
        })

    counts = {'pass': 0, 'fail': 0, 'unreadable': 0}
    for r in results:
        counts[r['status']] += 1
    return {
        'batch': str(root), 'preset': preset, 'strict': strict,
        'count': len(results), 'counts': counts, 'results': results,
    }



def render_batch(batch: dict, fail_list: bool) -> str:
    if fail_list:
        # 只输出要退的路径，一行一个 —— 方便直接复制去发通知。
        # 「读不了」也算要处理，但排在最后并加标记，避免被当成格式不合格。
        bad = [r['path'] for r in batch['results'] if r['status'] == 'fail']
        broken = [r['path'] for r in batch['results'] if r['status'] == 'unreadable']
        out = bad + [f'{p}  (读不了)' for p in broken]
        return '\n'.join(out)

    lines = [f'批量：{batch["batch"]}　预设：{batch["preset"]}　共 {batch["count"]} 份', '']
    width = max((len(r['file']) for r in batch['results']), default=10)
    for r in batch['results']:
        name = r['file'].ljust(min(width, 40))
        if r['status'] == 'pass':
            s = r['summary'] or {}
            lines.append(f'{name}  收      error {s.get("error", 0)} · warn {s.get("warn", 0)}')
        elif r['status'] == 'fail':
            s = r['summary'] or {}
            lines.append(f'{name}  要退    error {s.get("error", 0)} · warn {s.get("warn", 0)}')
        else:
            reason = str(r['verdict']).split('。')[0][:38]
            lines.append(f'{name}  读不了  {reason}')
    c = batch['counts']
    lines.append('')
    lines.append(f'汇总：{batch["count"]} 份 —— 收 {c["pass"]} · 要退 {c["fail"]} · 读不了 {c["unreadable"]}')
    if batch['strict']:
        lines.append('（--strict：warn 也计入要退）')
    return '\n'.join(lines)



def render(result: dict) -> str:
    lines: list[str] = []
    s = result['stats']
    lines.append(f'报告：{result["file"]}')
    lines.append(f'预设：{result["preset"]}　非空段落：{s["paragraphs"]}　表格：{s["tables"]}　图片：{s["images"]}')
    lines.append(f'已查：{"、".join(s["checked"]) or "（无）"}')
    lines.append('')

    if not result['findings']:
        lines.append(result['verdict'])
        return '\n'.join(lines)

    for f in result['findings']:
        head = f'[{f["id"]}] {f["kind"]}'
        if f.get('count', 1) > 1:
            head += f' × {f["count"]} 处'
        elif f['locator']:
            head += f' · {f["locator"]}'
        if f.get('count', 1) == 1 and f['excerpt']:
            head += f'「{f["excerpt"]}…」'
        lines.append(head)
        lines.append(f'    {f["problem"]}')
        if f.get('count', 1) > 1 and f.get('samples'):
            shown = f['samples'][:3]
            rest = len(f['samples']) - len(shown)
            for loc in shown:
                lines.append(f'    位置：{loc}')
            if rest > 0:
                lines.append(f'    （其余 {rest} 处见 --no-group）')
        lines.append(f'    修法：{f["fix"]}')

    lines.append('')
    grouped = sum(f.get('count', 1) - 1 for f in result['findings'])
    if grouped:
        lines.append(f'（已按同类问题合并，共 {result["stats"]["occurrences"]} 处；要逐条看用 --no-group）')
    lines.append(f'结论：{result["verdict"]}（error {result["summary"]["error"]} · '
                 f'warn {result["summary"]["warn"]} · info {result["summary"]["info"]}）')
    return '\n'.join(lines)



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='paper-format-check',
                                 description='论文 / 实验报告格式体检（只读，不改原件）')
    ap.add_argument('file', nargs='?', help='要检查的 .docx')
    ap.add_argument('--preset', default='实验报告', help='规则预设，默认「实验报告」')
    ap.add_argument('--rules', help='自定义规则文件（需含 presets 字段）')
    ap.add_argument('--student-id', default='', help='学号，用于核对页眉')
    ap.add_argument('--student-name', default='', help='姓名，用于核对页眉')
    ap.add_argument('--json', action='store_true', help='输出 JSON（永不截断）')
    ap.add_argument('--list-presets', action='store_true', help='列出可用预设')
    ap.add_argument('--show-rules', action='store_true', help='打印合并后实际生效的规则')
    ap.add_argument('--max-findings', type=int, default=50, help='人读输出最多显示多少条，默认 50')
    ap.add_argument('--strict', action='store_true', help='warn 也视为不通过（助教批改用）')
    ap.add_argument('--no-group', action='store_true',
                    help='人读输出不聚合，逐段列出每一处问题（JSON 始终是聚合形态）')
    ap.add_argument('--batch', metavar='目录', help='批量检查目录下所有 .docx（递归，跳过 ~$ 锁文件）')
    ap.add_argument('--fail-list', action='store_true',
                    help='只输出要退的文件路径，一行一个（配合 --batch）')
    ap.add_argument('--ignore', default='',
                    help='一次性豁免，逗号分隔：margins,headings 或 body.size,body.eastAsia')
    args = ap.parse_args(argv)

    if args.list_presets:
        src = Path(args.rules) if args.rules else Path(__file__).resolve().parent.parent / 'references' / 'rules.json'
        data = read_json(src)
        for name, spec in (data.get('presets') or {}).items():
            print(f'{name}：{spec.get("description", "")}')
        return 0

    rules, preset, _data = load_rules(args.preset, args.rules)

    ignored: list[str] = []
    if args.ignore:
        rules, ignored = apply_ignore(rules, args.ignore)

    if args.show_rules:
        print(json.dumps(rules, ensure_ascii=False, indent=2))
        return 0

    if args.batch:
        batch = run_batch(Path(args.batch), rules, preset, args.student_id, args.student_name,
                          args.strict, args.no_group)
        batch['ignored'] = ignored
        if args.json:
            print(json.dumps(batch, ensure_ascii=False, indent=2))
        else:
            print(render_batch(batch, args.fail_list))
        return 0 if batch['counts']['fail'] == 0 and batch['counts']['unreadable'] == 0 else 1

    if not args.file:
        ap.print_help()
        return 2

    result = run(Path(args.file), rules, preset, args.student_id, args.student_name,
                 args.strict, args.no_group)
    result['ignored'] = ignored

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        shown = result
        if len(result['findings']) > args.max_findings:
            # 按 severity 截断，不按检查项顺序 —— 否则后面的 error 会被前面的 warn 挤掉
            ranked = sorted(result['findings'],
                            key=lambda f: (SEVERITY_ORDER[f['severity']], f['id']))
            keep = {f['id'] for f in ranked[:args.max_findings]}
            shown = dict(result)
            shown['findings'] = [f for f in result['findings'] if f['id'] in keep]
            shown['truncated'] = len(result['findings']) - len(keep)
        out = render(shown)
        if shown.get('truncated'):
            out += f'\n（另有 {shown["truncated"]} 条未显示，用 --max-findings 调整；--json 不受截断影响）'
        print(out)

    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
