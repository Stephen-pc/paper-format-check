#!/usr/bin/env python3
"""docx 的 XML 层：把 .docx 读成「段落 + 每段的生效属性」。

只关心 OOXML 怎么读，不做任何判定 —— 判定全在 checks.py。
拆出来的理由见设计稿 §8.1：单文件过 800 行时，解析与判定混在一起，改一处要读全文。

零依赖：只用 Python 标准库（zipfile + xml.etree）。
"""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'

TWIPS_PER_CM = 1440 / 2.54

TWIPS_PER_CHAR_PT = 20  # 1pt = 20 缇

# 半磅 → 中文字号名。这张表是工具的脸面：
# 小四=12pt=24，四号=14pt=28，三号=16pt=32，小三=15pt=30，小二=18pt=36，
# 二号=22pt=44，小一=24pt=48，一号=26pt=52，小初=36pt=72，初号=42pt=84，
# 五号=10.5pt=21，小五=9pt=18，六号=7.5pt=15，小六=6.5pt=13，七号=5.5pt=11，八号=5pt=10

MONO_FONTS = ('consolas', 'courier', 'monaco', 'menlo', 'lucida console',
              'source code', 'jetbrains', 'fira code', 'cascadia', 'inconsolata')



def q(tag: str) -> str:
    return W + tag



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



def iter_sections(root):
    body = root.find(q('body'))
    if body is None:
        return []
    return list(body.iter(q('sectPr')))



def is_toc_like(p: Para) -> bool:
    """目录行的三个特征：TOC 样式、引导点、页码尾巴。"""
    style = str(p.props.get('_styleName', '') or p.props.get('_styleId', '')).lower()
    if 'toc' in style or '目录' in style:
        return True
    if re.search(r'\.{3,}|…{2,}|·{3,}', p.text):
        return True
    return False



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
