#!/usr/bin/env python3
"""paper-format-check —— 论文 / 实验报告格式体检（v0.3）。

只读：绝不修改原件。只查格式参数，不做内容审查。

三条判定原则（见设计稿 §8.3）：
  1. 读不到就不报 —— 属性缺失时报 info，不报 error；
  2. 不确定就降级 —— 文档网格、固定值行距、自动编号、SEQ 域，一律降级；
  3. 能定位才报 —— 每条 finding 必须指到「标题路径 + 段内序号 + 摘录」。

一条硬规矩（§7.6）：**本次真正比较过的检查项为 0 时，禁止判通过**。
注意口径是「真正比较过」，不是「函数被调用过」——全 null 的预设会让每个检查器都被调用
却一次比较都不做，那是比空预设更容易被写出来的形态。

退出码：0 通过，1 有问题（或 0 项检查，或 --strict 下有 warn），2 文件读不了。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
TWIPS_PER_CM = 1440 / 2.54
TWIPS_PER_CHAR_PT = 20  # 1pt = 20 缇

# 半磅 → 中文字号名。这张表是工具的脸面：
# 小四=12pt=24，四号=14pt=28，三号=16pt=32，小三=15pt=30，小二=18pt=36，
# 二号=22pt=44，小一=24pt=48，一号=26pt=52，小初=36pt=72，初号=42pt=84，
# 五号=10.5pt=21，小五=9pt=18，六号=7.5pt=15，小六=6.5pt=13，七号=5.5pt=11，八号=5pt=10
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
MONO_FONTS = ('consolas', 'courier', 'monaco', 'menlo', 'lucida console',
              'source code', 'jetbrains', 'fira code', 'cascadia', 'inconsolata')


def parse_numbering(text: str, pattern) -> list[int] | None:
    """把「图 2.3.1」解析成 [2, 3, 1]。"""
    m = pattern.match(text)
    if not m:
        return None
    return [int(x) for x in re.split(r'[.\-．]', m.group(1)) if x.strip()]
STUDENT_ID_RE = re.compile(r'\d{8,12}')


def q(tag: str) -> str:
    return W + tag


def die(msg: str, code: int = 2):
    """以指定退出码退出。`raise SystemExit("文本")` 实际返回 1，CI 分不清
    「文件坏了」和「格式不合格」，所以显式给码。"""
    print(f'paper-format-check: {msg}', file=sys.stderr)
    raise SystemExit(code)


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


# ---------------------------------------------------------------- 样式解析


def parse_styles(z: zipfile.ZipFile) -> tuple[dict[str, dict], dict]:
    """返回 (styleId → 直接属性, docDefaults 属性)。

    docDefaults 不能漏：它定义整篇默认字体字号，很多「没设置格式」的文档靠它生效。
    """
    styles: dict[str, dict] = {}
    defaults: dict = {}
    if 'word/styles.xml' not in z.namelist():
        return styles, defaults

    root = ET.fromstring(z.read('word/styles.xml'))

    dd = root.find(q('docDefaults'))
    if dd is not None:
        rpr = dd.find(q('rPrDefault'))
        if rpr is not None:
            inner = rpr.find(q('rPr'))
            if inner is not None:
                defaults.update(_read_rpr(inner))
        ppr = dd.find(q('pPrDefault'))
        if ppr is not None:
            inner = ppr.find(q('pPr'))
            if inner is not None:
                defaults.update(_read_ppr(inner))

    for st in root.iter(q('style')):
        sid = st.get(q('styleId'))
        if not sid:
            continue
        props: dict = {}
        name_el = st.find(q('name'))
        if name_el is not None:
            props['name'] = name_el.get(q('val')) or ''
        based = st.find(q('basedOn'))
        if based is not None:
            props['basedOn'] = based.get(q('val')) or ''
        ppr = st.find(q('pPr'))
        if ppr is not None:
            props.update(_read_ppr(ppr))
        rpr = st.find(q('rPr'))
        if rpr is not None:
            props.update(_read_rpr(rpr))
        styles[sid] = props
    return styles, defaults


def resolve_style(style_id: str, styles: dict[str, dict], _seen: set | None = None) -> dict:
    """沿 basedOn 链向上合并，近者胜。"""
    seen = _seen or set()
    if not style_id or style_id in seen or style_id not in styles:
        return {}
    seen.add(style_id)
    own = styles[style_id]
    parent = resolve_style(own.get('basedOn', ''), styles, seen)
    merged = dict(parent)
    merged.update({k: v for k, v in own.items() if k != 'basedOn'})
    return merged


# ---------------------------------------------------------------- 属性读取


def _read_ppr(ppr) -> dict:
    out: dict = {}
    ind = ppr.find(q('ind'))
    if ind is not None:
        if ind.get(q('firstLineChars')) is not None:
            out['firstLineChars'] = int(ind.get(q('firstLineChars'))) / 100
        if ind.get(q('firstLine')) is not None:
            out['firstLineTwips'] = int(ind.get(q('firstLine')))
        # 悬挂缩进：参考文献条目、词条、脚注式段落都用它。
        # 它等价于「有缩进，只是方向相反」，绝不能当成「没有首行缩进」。
        if ind.get(q('hangingChars')) is not None:
            out['hangingChars'] = int(ind.get(q('hangingChars'))) / 100
        if ind.get(q('hanging')) is not None:
            out['hangingTwips'] = int(ind.get(q('hanging')))
    spacing = ppr.find(q('spacing'))
    if spacing is not None:
        if spacing.get(q('line')) is not None:
            out['line'] = int(spacing.get(q('line')))
            out['lineRule'] = spacing.get(q('lineRule')) or 'auto'
    outline = ppr.find(q('outlineLvl'))
    if outline is not None:
        out['outlineLvl'] = int(outline.get(q('val')))
    if ppr.find(q('numPr')) is not None:
        out['numPr'] = True
    snap = ppr.find(q('snapToGrid'))
    if snap is not None:
        out['snapToGrid'] = snap.get(q('val')) not in ('0', 'false')
    return out


def _read_rpr(rpr) -> dict:
    out: dict = {}
    sz = rpr.find(q('sz'))
    if sz is not None and sz.get(q('val')):
        out['size'] = int(sz.get(q('val')))
    fonts = rpr.find(q('rFonts'))
    if fonts is not None:
        east = fonts.get(q('eastAsia'))
        if east:
            out['eastAsia'] = east
        ascii_font = fonts.get(q('ascii'))
        if ascii_font:
            out['ascii'] = ascii_font
    bold = rpr.find(q('b'))
    if bold is not None:
        out['bold'] = bold.get(q('val')) not in ('0', 'false')
    return out


def effective_props(p, styles: dict[str, dict], defaults: dict) -> dict:
    """四层合并：docDefaults → 样式链 → 段落直接格式 → run 直接格式。

    run 级取「第一个真正带字体格式的 run」作基准（不是最后一个）。
    段落末尾常挂一个英文或引用 run，取最后一个会让字号基准漂移。
    _mixedSizes 收集全段出现过的字号，只用于在文案里提示混排。
    """
    merged = dict(defaults)

    ppr = p.find(q('pPr'))
    style_id = ''
    direct: dict = {}
    if ppr is not None:
        st = ppr.find(q('pStyle'))
        if st is not None:
            style_id = st.get(q('val')) or ''
        direct.update(_read_ppr(ppr))

    from_style = resolve_style(style_id, styles)
    merged.update({k: v for k, v in from_style.items() if k != 'name'})

    sizes: list[int] = []
    base: dict | None = None
    for r in p.iter(q('r')):
        rpr = r.find(q('rPr'))
        if rpr is None:
            continue
        got = _read_rpr(rpr)
        if 'size' in got:
            sizes.append(got['size'])
        if base is None and ('size' in got or 'eastAsia' in got):
            base = got
    if base:
        merged.update(base)

    merged.update(direct)
    # 记录哪些属性是「段落/run 上直接写的」，用于判断「改样式一处即可」还是「逐段改」
    direct_keys = set(direct)
    if base:
        direct_keys |= {k for k in base if k in ('size', 'eastAsia')}
    merged['_direct'] = direct_keys
    merged['_styleId'] = style_id
    merged['_styleName'] = from_style.get('name', '')
    if len(set(sizes)) > 1:
        merged['_mixedSizes'] = sorted(set(sizes))
    return merged


# ---------------------------------------------------------------- 遍历


class Para:
    __slots__ = ('index', 'text', 'props', 'in_table', 'in_textbox', 'in_deleted', 'breaks', 'seq')

    def __init__(self, index, text, props, in_table, in_textbox, in_deleted, breaks, seq):
        self.index = index
        self.text = text
        self.props = props
        self.in_table = in_table
        self.in_textbox = in_textbox
        self.in_deleted = in_deleted
        self.breaks = breaks
        self.seq = seq


def walk_paragraphs(node, styles, defaults, state, counter) -> list[Para]:
    """按文档顺序收集段落，携带「表格/文本框/删除修订」上下文。

    修订痕迹按 run 级过滤：w:del 包的是 run 不是段落，段落级标记基本不触发。
    """
    out: list[Para] = []
    for child in node:
        tag = child.tag
        if tag == q('tbl'):
            out.extend(walk_paragraphs(child, styles, defaults, {**state, 'in_table': True}, counter))
        elif tag == q('txbxContent'):
            out.extend(walk_paragraphs(child, styles, defaults, {**state, 'in_textbox': True}, counter))
        elif tag == q('p'):
            # 序号只发给正文段落：表格与文本框里的段落在 Word 里不占正文段号，
            # 混进来会让 locator 的「第 N 段」跟用户看到的位置对不上。
            if state['in_table'] or state['in_textbox']:
                idx = None
            else:
                counter[0] += 1
                idx = counter[0]

            # 删除修订按 run 级过滤：先把 w:del / w:moveFrom 里的 w:t 收集成集合，
            # 再差集，避免对每个 w:t 都重扫整段（长段落上是 O(n²)）。
            skipped = set()
            for tag_skip in (q('del'), q('moveFrom')):
                for dead in child.iter(tag_skip):
                    for t in dead.iter(q('t')):
                        skipped.add(id(t))
            text = ''.join(t.text or '' for t in child.iter(q('t')) if id(t) not in skipped)

            props = effective_props(child, styles, defaults)
            breaks = len([b for b in child.iter(q('br'))])
            # SEQ 域有两种写法：复杂域用 w:instrText，简单域用 w:fldSimple/@w:instr
            seq = any('SEQ' in (t.text or '') for t in child.iter(q('instrText')))
            if not seq:
                seq = any('SEQ' in (fs.get(q('instr')) or '') for fs in child.iter(q('fldSimple')))
            out.append(Para(idx, text.strip(), props, state['in_table'],
                            state['in_textbox'], state['in_deleted'], breaks, seq))
            # 段落内部可能嵌文本框（图形里的文字），继续下钻但标成 textbox
            out.extend(walk_paragraphs(child, styles, defaults, {**state, 'in_textbox': True}, counter))
    return out


def is_code_like(p: Para) -> bool:
    """等宽字体或代码样式 → 代码块。

    代码块的排版规则与正文完全不同：没有首行缩进、常用五号、行距按固定值。
    对它套正文规则会稳定产生大批误报 —— 真实报告里 63 条「没有首行缩进」全落在这儿。
    """
    font = (str(p.props.get('ascii') or '') + str(p.props.get('eastAsia') or '')).lower()
    if any(m in font for m in MONO_FONTS):
        return True
    style = str(p.props.get('_styleName', '') or p.props.get('_styleId', '')).lower()
    return 'code' in style or '代码' in style


def is_heading(props: dict, text: str) -> bool:
    if props.get('outlineLvl') is not None and props['outlineLvl'] < 9:
        return True
    for source in (props.get('_styleName', ''), props.get('_styleId', '')):
        if re.match(r'^(heading|标题)\s*\d', str(source).lower()):
            return True
    return False


def heading_level(props: dict) -> int:
    if props.get('outlineLvl') is not None and props['outlineLvl'] < 9:
        return props['outlineLvl'] + 1
    for source in (props.get('_styleName', ''), props.get('_styleId', '')):
        m = re.search(r'(\d)', str(source))
        if m:
            return int(m.group(1))
    return 1


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


def iter_sections(root):
    body = root.find(q('body'))
    if body is None:
        return []
    return list(body.iter(q('sectPr')))


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


def is_toc_like(p: Para) -> bool:
    """目录行的三个特征：TOC 样式、引导点、页码尾巴。"""
    style = str(p.props.get('_styleName', '') or p.props.get('_styleId', '')).lower()
    if 'toc' in style or '目录' in style:
        return True
    if re.search(r'\.{3,}|…{2,}|·{3,}', p.text):
        return True
    return False


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


def count_body_images(root) -> int:
    """只数正文里真正被引用的图片。

    两条限制缺一不可：
      · 不能数 word/media/：页眉里的校徽也在那儿，学位论文模板几乎必带 logo，
        于是「图片数 > 0」恒成立，纯文字文档会被要求补图题；
      · 不能把任意 r:id 都算成图片：headerReference / footerReference 也用 r:id，
        那样每份文档都会凭空多出一张「图片」。
    """
    ids = set()
    for el in root.iter():
        rid = el.get(R + 'embed')
        if rid:
            ids.add(rid)
        # VML 旧式图片用 r:id，但只认 imagedata 元素
        if el.tag.endswith('}imagedata') and el.get(R + 'id'):
            ids.add(el.get(R + 'id'))
    return len(ids)


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
