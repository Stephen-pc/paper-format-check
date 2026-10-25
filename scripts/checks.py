#!/usr/bin/env python3
"""判定层：每条规则一个检查器，输入「段落 + 规则」，输出 findings。

依赖 ooxml 提供的段落与属性，本文件不做任何 XML 解析。
三条判定原则（设计稿 §8.3）：读不到就不报、不确定就降级、能定位才报。
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from ooxml import (
    TWIPS_PER_CM,
    TWIPS_PER_CHAR_PT,
    Para,
    heading_level,
    is_code_like,
    is_heading,
    is_toc_like,
    iter_sections,
    q,
)

FONT_SIZE_NAMES = {
    84: '初号', 72: '小初', 52: '一号', 48: '小一', 44: '二号', 36: '小二',
    32: '三号', 30: '小三', 28: '四号', 24: '小四', 21: '五号', 18: '小五',
    15: '六号', 13: '小六', 11: '七号', 10: '八号',
}

# 检查项固定优先序：学生只看前三条，所以前三条必须是「不改就退」的

CHECK_ORDER = ['header', 'margins', 'bodyRange', 'body', 'breaks', 'captions', 'headings']

# 图号支持任意级数：图 1、图 1.1、图 2.3.1 都认。
# 只认两级时，「图 2.3.1」会被读成「第 2 章第 3 张」，下一张「图 2.3.2」还是 3 → 判成断档。

FIG_RE = re.compile(r'^图\s*(\d+(?:\s*[.\-．]\s*\d+)*)')

TAB_RE = re.compile(r'^表\s*(\d+(?:\s*[.\-．]\s*\d+)*)')

# 等宽字体说明这是代码块。代码块不该要求首行缩进、中文字体、1.5 倍行距。

def parse_numbering(text: str, pattern) -> list[int] | None:
    """把「图 2.3.1」解析成 [2, 3, 1]。"""
    m = pattern.match(text)
    if not m:
        return None
    return [int(x) for x in re.split(r'[.\-．]', m.group(1)) if x.strip()]

STUDENT_ID_RE = re.compile(r'\d{8,12}')



def describe_size(half_points: int | None) -> str:
    if half_points is None:
        return '未显式设置'
    pt = half_points / 2
    name = FONT_SIZE_NAMES.get(half_points)
    return f'{name} {pt:g}pt' if name else f'{pt:g}pt'



def twips_to_cm(twips: int) -> float:
    return round(twips / TWIPS_PER_CM, 2)



def has_any(section, keys) -> bool:
    """section 里这些键至少有一个非 None 且非 False 才算「有要求」。

    这是 §7.6 的判定基础：预设写成 {size: null, lineSpacing: null} 时，
    检查器会被调用，但一次比较都不该做，也就不该登记 checked。
    """
    if not isinstance(section, dict):
        return False
    for k in keys:
        v = section.get(k)
        if v is None or v is False:
            continue
        if isinstance(v, (list, tuple)) and not v:
            continue
        return True
    return False



# ---------------------------------------------------------------- 报告


class Report:
    def __init__(self):
        self.findings: list[dict] = []
        self.executed: list[str] = []

    def ran(self, kind: str):
        if kind not in self.executed:
            self.executed.append(kind)

    def add(self, kind, severity, problem, fix, locator='', paragraph=None, excerpt='',
            actual=None, expected=None, style_only=False, style_name=''):
        self.findings.append({
            'kind': kind, 'severity': severity, 'locator': locator, 'paragraph': paragraph,
            'excerpt': excerpt, 'actual': actual, 'expected': expected,
            'problem': problem, 'fix': fix,
            'style_only': bool(style_only), 'style_name': style_name or '',
        })



def locator_of(para: Para, heading_path: str) -> str:
    return f'{heading_path or "文档开头"} · 第 {para.index} 段'



# ---------------------------------------------------------------- 检查器


def check_header(z, rules, report: Report, sid: str, sname: str):
    want = rules.get('header') or {}
    if not has_any(want, ('requireStudentId', 'requireStudentName')):
        return
    report.ran('header')

    texts: list[str] = []
    for name in z.namelist():
        if re.match(r'word/header\d*\.xml$', name):
            root = ET.fromstring(z.read(name))
            texts.append(''.join(t.text or '' for t in root.iter(q('t'))))
    header_text = ' '.join(t.strip() for t in texts if t.strip()).strip()

    if not header_text:
        report.add('header', 'error', '没有任何页眉内容，要求页眉带学号与姓名',
                   '双击页面顶部进入页眉 → 输入「学号 姓名」')
        return

    if want.get('requireStudentId'):
        if sid:
            if sid not in header_text:
                report.add('header', 'error', f'页眉里找不到学号 {sid}（当前页眉：{header_text[:40]}）',
                           f'双击页眉 → 补上学号 {sid}')
        elif not STUDENT_ID_RE.search(header_text):
            report.add('header', 'error', f'页眉里没有学号（当前页眉：{header_text[:40]}）',
                       '双击页眉 → 补上学号')

    if want.get('requireStudentName'):
        if sname:
            if sname not in header_text:
                report.add('header', 'error', f'页眉里找不到姓名 {sname}（当前页眉：{header_text[:40]}）',
                           f'双击页眉 → 补上姓名 {sname}')
        else:
            # 没传姓名就不假装核对过：报 info，不报 error，也不静默放过
            report.add('header', 'info',
                       f'未提供姓名，只核对了学号（当前页眉：{header_text[:40]}）',
                       '需要核对姓名时加 --student-name 你的姓名')



def check_margins(root, rules, report: Report):
    want = rules.get('margins')
    if not has_any(want, ('top', 'bottom', 'left', 'right')):
        return
    report.ran('margins')

    sections = iter_sections(root)
    if not sections:
        report.add('margins', 'info', '文档里找不到分节属性，无法核对页边距',
                   '若需核对，请在 Word 里检查页面设置')
        return

    for sec_no, sect in enumerate(sections, start=1):
        pg = sect.find(q('pgMar'))
        if pg is None:
            continue
        where = f'第 {sec_no} 节' if len(sections) > 1 else '全文'
        for key, label in (('top', '上'), ('bottom', '下'), ('left', '左'), ('right', '右')):
            raw = pg.get(q(key))
            want_cm = want.get(key)
            if raw is None or want_cm is None:
                continue
            actual_cm = twips_to_cm(int(raw))
            if abs(actual_cm - want_cm) > 0.1:
                report.add('margins', 'error',
                           f'{where}页边距{label} {actual_cm:g}cm，要求 {want_cm:g}cm',
                           f'布局 → 页边距 → 自定义，{label}边距改为 {want_cm:g}cm',
                           locator=where, actual=actual_cm, expected=want_cm)



def resolve_body_range(paras: list[Para], rules: dict) -> tuple[list[Para], str]:
    """定正文边界。

    两个约束，缺一个就会在真实文档上炸：
      · 起点只在**标题段落**上匹配。目录行「一、实验目的 ...... 1」文本上就是标题，
        若允许在任意段落上 search，正文起点会落在目录内部，于是整份目录被当成正文，
        逐行报字号/缩进 —— 这正是 v0.2 补丁想解决却被 fixture 太小掩盖掉的问题；
      · 命中点还要排除 TOC 特征：TOC 样式、引导点（连续 3 个以上句点或省略号）。
    """
    spec = rules.get('bodyRange') or {}
    start_re = spec.get('startAfter')
    end_re = spec.get('endAt')
    if not start_re and not end_re:
        return paras, '未配置正文边界，按全篇检查'

    start_idx = 0
    note = ''
    if start_re:
        pat = re.compile(start_re)
        hit = None
        for i, p in enumerate(paras):
            if not is_heading(p.props, p.text):
                continue
            if is_toc_like(p):
                continue
            if pat.search(p.text):
                hit = i
                break
        if hit is not None:
            start_idx = hit + 1
        else:
            # 关键词没命中时**不能退化为全篇** —— 全篇不等于正文。
            # 退化为全篇等于把封面、目录、参考文献重新请回检查范围，
            # 而这三块恰恰是 v0.2 引入 bodyRange 要挡的东西；
            # 而且任何没被列进正则的文档都会走这条路，换一个开头短语就重演。
            # 改成「从第一个标题之后开始」，并叠加已有的 TOC 特征排除。
            start_idx, why = _fallback_start(paras)
            note = f'正文起点没匹配到，{why}'

    end_idx = len(paras)
    if end_re:
        pat = re.compile(end_re)
        for i in range(start_idx, len(paras)):
            # 与 startAfter 对称：终点也只在标题段落上匹配。
            # 「参考文献的数量和质量决定了报告的可信度」「附件中给出了完整数据」
            # 都是正常正文起句，若按任意段落匹配，检查范围会被悄悄截短，
            # 然后输出「全部通过」—— 这比误报更坏。
            if not is_heading(paras[i].props, paras[i].text):
                continue
            if is_toc_like(paras[i]):
                continue
            if pat.search(paras[i].text):
                end_idx = i
                break

    return paras[start_idx:end_idx], note



def _fallback_start(paras: list[Para]) -> tuple[int, str]:
    """起点退化口径：max(第一个标题之后, 最后一个 TOC 特征段落之后)。

    为什么不是「全篇」：封面是正文样式的大字标题、目录是带引导点的行，
    两者都不是「正文」，按全篇检查会稳定报十几条字号/行距错。
    """
    first_heading = None
    last_toc = None
    for i, p in enumerate(paras):
        if p.in_table or p.in_textbox or p.in_deleted or not p.text:
            continue
        if first_heading is None and is_heading(p.props, p.text) and not is_toc_like(p):
            first_heading = i
        if is_toc_like(p):
            last_toc = i

    candidates = [x for x in (first_heading, last_toc) if x is not None]
    if not candidates:
        return 0, '也没找到标题段落，已按全篇检查'
    return max(candidates) + 1, '已从第一个标题之后开始检查（已排除封面与目录）'



BODY_KEYS = ('size', 'firstLineChars', 'lineSpacing', 'eastAsia')



def check_body(paras, rules, report: Report, grid_pitch: int | None, heading_of):
    want = rules.get('body') or {}
    if not has_any(want, BODY_KEYS):
        return
    report.ran('body')

    expect_size = want.get('size')
    expect_indent = want.get('firstLineChars')
    expect_line = want.get('lineSpacing')
    expect_font = want.get('eastAsia')
    allow_fixed_line = bool(want.get('allowFixedLineSpacing'))
    skip_styles = {s.lower() for s in (rules.get('skipStyles') or [])}

    for p in paras:
        if not p.text or p.in_table or p.in_textbox or p.in_deleted:
            continue
        if is_heading(p.props, p.text):
            continue
        if FIG_RE.match(p.text) or TAB_RE.match(p.text):
            continue
        if str(p.props.get('_styleName', '')).lower() in skip_styles:
            continue
        if p.props.get('numPr'):
            continue
        if is_code_like(p):
            continue

        loc = locator_of(p, heading_of(p.index))
        excerpt = p.text[:24]
        direct = p.props.get('_direct') or set()
        style_name = p.props.get('_styleName') or p.props.get('_styleId') or ''

        def only_style(key: str) -> bool:
            # 该属性不是段落/run 上直接写的 → 来自样式，改样式一处即可
            return bool(style_name) and key not in direct

        if expect_size is not None:
            actual = p.props.get('size')
            allowed = expect_size if isinstance(expect_size, list) else [expect_size]
            if actual is None:
                report.add('body', 'info', '该段未显式设置字号，实际由样式或文档默认值决定',
                           '若视觉上字号不对，请检查所用样式的字号', loc, p.index, excerpt,
                           None, allowed[0])
            elif actual not in allowed:
                note = ''
                if p.props.get('_mixedSizes'):
                    note = f'（该段存在多种字号：{"、".join(describe_size(s) for s in p.props["_mixedSizes"])}）'
                report.add('body', 'error',
                           f'字号 {describe_size(actual)}，要求 {describe_size(allowed[0])}{note}',
                           f'选中该段 → 字号改为 {describe_size(allowed[0])}',
                           loc, p.index, excerpt, actual, allowed[0],
                           style_only=only_style('size'), style_name=style_name)

        if expect_indent:
            chars = p.props.get('firstLineChars')
            twips = p.props.get('firstLineTwips')
            hanging = p.props.get('hangingChars') or p.props.get('hangingTwips')
            if hanging:
                # 悬挂缩进是合法排版（参考文献、词条、脚注式段落），
                # 报「没有首行缩进」是事实错误，而且文案给的修法会把版面改坏。
                # 只跳过缩进这一项，行距与字体照常检查。
                pass
            elif chars is None and twips is None:
                report.add('body', 'error', f'没有首行缩进，要求缩进 {expect_indent:g} 字符',
                           '选中该段 → 段落 → 特殊格式选「首行缩进」，缩进值 2 字符',
                           loc, p.index, excerpt, 0, expect_indent)
            elif chars is None and twips is not None:
                size = p.props.get('size') or (expect_size if isinstance(expect_size, int) else None)
                if size:
                    derived = twips / (size / 2 * TWIPS_PER_CHAR_PT)
                    if abs(derived - expect_indent) > 0.3:
                        report.add('body', 'warn',
                                   f'首行缩进折算约 {derived:.1f} 字符（原文 {twips} 缇），要求 {expect_indent:g} 字符',
                                   '段落 → 特殊格式 → 首行缩进，直接用字符单位设置',
                                   loc, p.index, excerpt, round(derived, 1), expect_indent)
                else:
                    report.add('body', 'info',
                               f'首行缩进只写了绝对值 {twips} 缇，字号未知无法折算成字符',
                               '段落 → 特殊格式 → 首行缩进，改用字符单位', loc, p.index, excerpt)
            elif chars is not None and abs(chars - expect_indent) > 0.05:
                report.add('body', 'error', f'首行缩进 {chars:g} 字符，要求 {expect_indent:g} 字符',
                           f'段落 → 特殊格式 → 首行缩进，改为 {expect_indent:g} 字符',
                           loc, p.index, excerpt, chars, expect_indent)

        if expect_line:
            want_raw = int(round(expect_line * 240))
            actual = p.props.get('line')
            rule = p.props.get('lineRule', 'auto')
            if actual is None:
                report.add('body', 'warn', f'行距未显式设置（默认单倍），要求 {expect_line:g} 倍',
                           f'段落 → 行距 → 选倍数，设为 {expect_line:g}', loc, p.index, excerpt,
                           None, expect_line)
            elif rule != 'auto':
                # 预设声明「允许固定值行距」时直接放过：有些学院的要求就是
                # 「固定值 20 磅」，这时按倍数判定是拿错尺子。
                if allow_fixed_line:
                    continue
                # 只有「与网格线距一致」或「段落显式吸附网格」时才认为网格导致，
                # 其余固定值仍是真问题 —— 中文文档几乎都带 docGrid，放宽到一律降级
                # 会让这一类永远不报。
                grid_explains = grid_pitch is not None and (
                    actual == grid_pitch or p.props.get('snapToGrid') is True
                )
                if grid_explains:
                    report.add('body', 'info',
                               f'文档启用了网格（docGrid 行距 {grid_pitch}），该段行距 {actual} 与网格一致，'
                               f'属网格对齐的正常表现，未判为错误',
                               '若确认行距不对，请在段落设置里改为倍数行距', loc, p.index, excerpt)
                else:
                    report.add('body', 'warn',
                               f'行距用的是固定值（{rule} {actual} 缇），要求 {expect_line:g} 倍行距',
                               f'段落 → 行距 → 改为倍数 {expect_line:g}；若学院允许固定值可忽略本条',
                               loc, p.index, excerpt, actual, want_raw)
            elif abs(actual - want_raw) > 12:
                report.add('body', 'error',
                           f'行距约 {actual / 240:.2f} 倍，要求 {expect_line:g} 倍',
                           f'段落 → 行距 → 倍数，设为 {expect_line:g}',
                           loc, p.index, excerpt, actual, want_raw,
                           style_only=only_style('line'), style_name=style_name)

        if expect_font:
            font = p.props.get('eastAsia')
            if font and font != expect_font:
                report.add('body', 'warn', f'中文字体是 {font}，要求 {expect_font}',
                           f'选中该段 → 字体改为 {expect_font}', loc, p.index, excerpt, font, expect_font,
                           style_only=only_style('eastAsia'), style_name=style_name)



def check_breaks(paras, rules, report: Report, heading_of):
    # 只有显式开启才跑。不能靠「配置了 body」蹭一条 checked —— 那会让全 null 的
    # 预设混过 §7.6。
    if rules.get('checkBreaks') is not True:
        return
    report.ran('breaks')

    min_run = int(rules.get('blankRunThreshold') or 3)

    for p in paras:
        if p.in_table or p.in_textbox or p.in_deleted:
            continue
        if p.breaks > 0 and p.text:
            report.add('breaks', 'warn',
                       f'该段含 {p.breaks} 处手动换行（Shift+Enter），不是真正的段落，行距与缩进会失真',
                       '把 Shift+Enter 换行改为回车分段；确需换行的地方用「与下段同页」或调整段间距',
                       locator_of(p, heading_of(p.index)), p.index, p.text[:24], p.breaks)

    run_start, run_len = None, 0

    def flush():
        if run_len >= min_run:
            report.add('breaks', 'warn',
                       f'第 {run_start} 段起连续 {run_len} 个空段落，用空行撑间距会让分页与行距失控',
                       '删掉空段落，改用段前/段后间距', f'第 {run_start} 段附近', run_start, '', run_len)

    for p in paras:
        if p.in_table or p.in_textbox or p.in_deleted:
            continue
        if not p.text:
            if run_start is None:
                run_start = p.index
            run_len += 1
        else:
            flush()
            run_start, run_len = None, 0
    flush()



def check_caption_series(paras, report: Report, heading_of, pattern, label, scheme,
                         media_count: int, require_missing: bool):
    """图号 / 表号的连续性。

    编号支持任意级数（图 1 / 图 1.1 / 图 2.3.1）。连续性按**父级分组**核对：
    末位必须在同一父级下从 1 连续递增；父级变化本身不算断档。
    """
    items: list[tuple[list[int], Para]] = []
    for p in paras:
        if p.in_deleted or p.in_table or p.in_textbox:
            continue
        path = parse_numbering(p.text, pattern)
        if path:
            items.append((path, p))

    if not items:
        if require_missing and media_count > 0:
            if label == '图':
                what = f'文档里有 {media_count} 张图片，但没有任何「图 1.1」形式的图题'
                fix = '在每张图下方补图题，格式「图 章号.序号 说明」'
            else:
                what = f'文档里有 {media_count} 个表格，但没有任何「表 1.1」形式的表题'
                fix = '在每个表格上方补表题，格式「表 章号.序号 说明」'
            report.add('captions', 'warn', what, fix)
        return

    if scheme == 'none':
        return

    seq_paras = [p for _path, p in items if p.seq]
    if seq_paras:
        report.add('captions', 'info',
                   f'{label}题使用了 SEQ 域自动编号，域结果可能未更新，无法可靠核对连续性',
                   '全选文档后按 F9 更新域，再重新检查',
                   locator_of(seq_paras[0], heading_of(seq_paras[0].index)), seq_paras[0].index,
                   seq_paras[0].text[:24])
        return

    depths = {len(path) for path, _p in items}
    majority = None
    if len(depths) > 1:
        counts = {d: sum(1 for x, _ in items if len(x) == d) for d in depths}
        majority = max(counts, key=lambda d: counts[d])
        report.add('captions', 'info',
                   f'{label}题编号级数不一致：' + '、'.join(
                       f'{d} 级 {counts[d]} 个' for d in sorted(depths))
                   + f'，连续性只按占多数的 {majority} 级核对',
                   f'统一成一种级数，例如全部用「{label} 章号.序号」')

    def depth_ok(depth: int) -> bool:
        # 级数混用时只查占多数的那一支：少数派的「断档」其实是混用造成的假象，
        # 逐条报出来只会把同一个根因重复成 N 条 error。
        if majority is not None:
            return depth == majority
        if scheme == 'any':
            return True
        if scheme == 'global':
            return depth == 1
        return depth >= 2

    groups: dict[tuple[int, ...], list[tuple[int, Para]]] = {}
    for path, p in items:
        if not depth_ok(len(path)):
            continue
        groups.setdefault(tuple(path[:-1]), []).append((path[-1], p))

    for parent, seqs in groups.items():
        prefix = '.'.join(str(x) for x in parent)
        expected = 1
        for last, p in seqs:
            if last != expected:
                want = f'{prefix}.{expected}' if prefix else f'{expected}'
                got = f'{prefix}.{last}' if prefix else f'{last}'
                report.add('captions', 'error',
                           f'{label} {got} 序号断档：按顺序这里应为 {label} {want}',
                           f'把该{label}题改为「{label} {want}」，并把后续顺延',
                           locator_of(p, heading_of(p.index)), p.index, p.text[:24], got, want)
            expected = max(expected, last) + 1

    if scheme == 'chapter':
        shallow = [p for path, p in items if len(path) == 1]
        if shallow:
            report.add('captions', 'info',
                       f'有 {len(shallow)} 个{label}题用的是全文连续编号（如「{label} 1」），本预设要求分章编号',
                       f'若学院接受全文连续编号，把预设里的 captions.scheme 改为 any')



def check_captions(paras, rules, report: Report, heading_of, image_count: int, table_count: int):
    want = rules.get('captions') or {}
    scheme = want.get('scheme') or 'any'
    need_fig = bool(want.get('requireFigureCaption'))
    need_tab = bool(want.get('requireTableCaption'))

    # scheme=none 且两个 require 都为假时，这个检查器一步比较都不会做。
    # 不登记 checked —— 否则「只有 none」的预设会靠一条空登记混过 §7.6。
    if scheme == 'none' and not need_fig and not need_tab:
        return
    if not has_any(want, ('scheme', 'requireFigureCaption', 'requireTableCaption')):
        return
    report.ran('captions')

    check_caption_series(paras, report, heading_of, FIG_RE, '图', scheme, image_count, need_fig)
    check_caption_series(paras, report, heading_of, TAB_RE, '表', scheme, table_count, need_tab)




def check_headings(paras, rules, report: Report):
    want = rules.get('headings') or {}
    levels = {k: v for k, v in want.items() if isinstance(v, dict)}
    if not any(has_any(v, ('size', 'bold', 'eastAsia')) for v in levels.values()):
        return
    report.ran('headings')

    for p in paras:
        if not p.text or p.in_table or p.in_textbox or p.in_deleted:
            continue
        if not is_heading(p.props, p.text):
            continue
        spec = levels.get(str(heading_level(p.props)))
        if not spec:
            continue
        loc = f'第 {heading_level(p.props)} 级标题 · 第 {p.index} 段'
        if spec.get('size') is not None and p.props.get('size') not in (None, spec['size']):
            # 标题字号回到 warn：它的正确值取决于预设，而预设只是猜测 ——
            # error 应该留给「与预设无关也一定错」的项（缺页眉、页边距不符、文档损坏）。
            # 要阻塞就交给 --strict，助教本来就用它。
            report.add('headings', 'warn',
                       f'标题字号 {describe_size(p.props.get("size"))}，要求 {describe_size(spec["size"])}',
                       f'选中标题 → 字号改为 {describe_size(spec["size"])}',
                       loc, p.index, p.text[:24], p.props.get('size'), spec['size'])
        if spec.get('bold') and p.props.get('bold') is False:
            report.add('headings', 'warn', '标题未加粗，要求加粗', '选中标题 → Ctrl+B',
                       loc, p.index, p.text[:24])
        want_font = spec.get('eastAsia')
        font = p.props.get('eastAsia')
        if want_font and font and font != want_font:
            report.add('headings', 'warn', f'标题中文字体是 {font}，要求 {want_font}',
                       f'选中标题 → 字体改为 {want_font}', loc, p.index, p.text[:24], font, want_font)
