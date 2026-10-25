#!/usr/bin/env python3
"""生成 tests/fixtures 下的 docx 样本。

为什么用脚本生成而不是提交二进制：二进制 fixture 改不动、也 review 不了。
这个脚本本身就是「样本规格」的说明书 —— 每个 fixture 想测什么，写在函数名和注释里。

用法：
    python tests/make-fixtures.py

每个 fixture 的期望结果见 tests/EXPECTATIONS.md。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent / 'fixtures'

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
NS_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
NS_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'

CT = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
</Types>'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {NS}>
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/></w:style>
</w:styles>'''

PNG = b'\x89PNG\r\n\x1a\n'


def p(text, size=None, chars=None, twips=None, line=None, rule='auto', style=None,
      font=None, numpr=False, br=0, empty=False, image=None, snap=None, seq=False,
      hang=None, mono=None):
    """一个段落。image 是关系 id，给了就插一张内嵌图。"""
    ppr = ''
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if numpr:
        ppr += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    if snap is not None:
        ppr += f'<w:snapToGrid w:val="{1 if snap else 0}"/>'
    if hang is not None:
        ppr += f'<w:ind w:hangingChars="{int(hang * 100)}"/>'
    elif chars is not None:
        ppr += f'<w:ind w:firstLineChars="{int(chars * 100)}"/>'
    elif twips is not None:
        ppr += f'<w:ind w:firstLine="{twips}"/>'
    if line is not None:
        ppr += f'<w:spacing w:line="{line}" w:lineRule="{rule}"/>'

    rpr = ''
    if size or font or mono:
        inner = ''
        if mono:
            inner += f'<w:rFonts w:ascii="{mono}" w:hAnsi="{mono}" w:eastAsia="宋体"/>'
        elif font:
            inner += f'<w:rFonts w:eastAsia="{font}"/>'
        if size:
            inner += f'<w:sz w:val="{size}"/>'
        rpr = f'<w:rPr>{inner}</w:rPr>'

    if empty:
        return f'<w:p><w:pPr>{ppr}</w:pPr></w:p>'

    if image:
        run = (f'<w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
               f'<a:graphic {NS_A}><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
               f'<a:blip r:embed="{image}"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')
        return f'<w:p><w:pPr>{ppr}</w:pPr>{run}</w:p>'

    if seq:
        run = (f'<w:r>{rpr}<w:fldSimple w:instr=" SEQ 图 \\* ARABIC ">'
               f'<w:r><w:t>{text}</w:t></w:r></w:fldSimple></w:r>')
        return f'<w:p><w:pPr>{ppr}</w:pPr>{run}</w:p>'

    if br:
        run = f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t><w:br/></w:r>'
    else:
        run = f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'
    return f'<w:p><w:pPr>{ppr}</w:pPr>{run}</w:p>'


def sect_break(margins=(1440, 1440, 1701, 1417), grid=None):
    """段落级分节符：这一节在**这个段落处**结束，用它自己的页边距。"""
    top, bottom, left, right = margins
    grid_xml = f'<w:docGrid w:type="lines" w:linePitch="{grid}"/>' if grid else ''
    return (f'<w:p><w:pPr><w:sectPr>{grid_xml}'
            f'<w:pgSz w:w="11906" w:h="16838"/>'
            f'<w:pgMar w:top="{top}" w:bottom="{bottom}" w:left="{left}" w:right="{right}"/>'
            f'</w:sectPr></w:pPr></w:p>')


def build(body_xml, *, margins=(1440, 1440, 1701, 1417), grid=None, header='2411020125 彭超',
          header_image=False, body_images=()):
    """拼一个最小 docx 的各个部件。"""
    top, bottom, left, right = margins
    grid_xml = f'<w:docGrid w:type="lines" w:linePitch="{grid}"/>' if grid else ''
    sect = (f'<w:sectPr>'
            f'<w:headerReference w:type="default" r:id="rIdH" {NS_R}/>'
            f'{grid_xml}'
            f'<w:pgSz w:w="11906" w:h="16838"/>'
            f'<w:pgMar w:top="{top}" w:bottom="{bottom}" w:left="{left}" w:right="{right}"/>'
            f'</w:sectPr>')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                f'<w:document {NS} {NS_R}><w:body>{body_xml}{sect}</w:body></w:document>')

    rels = ['<Relationship Id="rIdH" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>']
    media = {}
    for i, rid in enumerate(body_images, start=1):
        rels.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image{i}.png"/>')
        media[f'word/media/image{i}.png'] = PNG
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + ''.join(rels) + '</Relationships>')

    hdr_rels = ''
    if header_image:
        # 页眉里的校徽：只出现在 header 部件，不该被算进「正文图片」
        hdr_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rIdLogo" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/logo.png"/>'
                    '</Relationships>')
        media['word/media/logo.png'] = PNG
        logo = (f'<w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
                f'<a:graphic {NS_A}><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                f'<a:blip r:embed="rIdLogo"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')
    else:
        logo = ''

    header_xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                  f'<w:hdr {NS} {NS_R}><w:p>{logo}<w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>')

    return {
        '[Content_Types].xml': CT,
        '_rels/.rels': RELS,
        'word/document.xml': document,
        'word/styles.xml': STYLES,
        'word/_rels/document.xml.rels': doc_rels,
        'word/header1.xml': header_xml,
        'word/_rels/header1.xml.rels': hdr_rels,
        **media,
    }


def write(name, parts):
    path = OUT / name
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for part, content in parts.items():
            if not content:
                continue
            z.writestr(part, content)
    print(f'  {name}')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f'写入 {OUT}')

    # 1 全合规：小四、2 字符缩进、1.5 倍、图题 1.1/1.2 连续
    body = p('一、实验目的', style='Heading1')
    body += p('掌握 HBase 的安装与基本操作。', size=24, chars=2, line=360)
    body += p('二、实验内容', style='Heading1')
    body += p('完成集群部署与 shell 操作。', size=24, chars=2, line=360)
    body += p('图 1.1 集群拓扑', chars=0, line=360, style='Caption')
    body += p('图 1.2 shell 输出', chars=0, line=360, style='Caption')
    write('clean.docx', build(body))

    # 2 多处违规：五号、无缩进、单倍行距、页眉缺学号、图号断档
    body = p('一、实验目的', style='Heading1')
    body += p('这一段的字号是五号，也没有首行缩进。', size=21, line=240)
    body += p('这一段的字体是黑体，行距单倍。', size=24, font='黑体', line=240)
    body += p('图 1.1 集群拓扑', chars=0, line=360, style='Caption')
    body += p('图 1.3 跳号了', chars=0, line=360, style='Caption')
    write('bad.docx', build(body, header='数据库实验报告'))

    # 3 网格与行距一致（312/312）：应降级为 info，不报 error
    body = p('一、实验目的', style='Heading1')
    body += p('网格文档里的固定值行距。', size=24, chars=2, line=312, rule='exact')
    body += p('第二段同样。', size=24, chars=2, line=312, rule='exact')
    write('grid.docx', build(body, grid=312))

    # 4 网格与行距不一致（312/400）：网格不能解释，仍按固定值 warn
    body = p('一、实验目的', style='Heading1')
    body += p('网格是 312，但行距被设成固定值 400。', size=24, chars=2, line=400, rule='exact')
    write('grid-mismatch.docx', build(body, grid=312))

    # 5 无网格的固定值行距：真问题，应 warn
    body = p('一、实验目的', style='Heading1')
    body += p('没有网格，行距是固定值 400。', size=24, chars=2, line=400, rule='exact')
    write('fixed-line.docx', build(body))

    # 6 手动换行 + 连续空段落
    body = p('一、实验目的', style='Heading1')
    body += p('第一行手动换行', size=24, chars=2, line=360, br=1)
    body += p('', empty=True)
    body += p('', empty=True)
    body += p('', empty=True)
    body += p('正文继续。', size=24, chars=2, line=360)
    write('breaks.docx', build(body))

    # 7 正文有图但没图题
    body = p('一、实验目的', style='Heading1')
    body += p('下图是集群拓扑。', size=24, chars=2, line=360)
    body += p('', image='rIdImg1')
    body += p('', image='rIdImg2')
    write('nocaption.docx', build(body, body_images=('rIdImg1', 'rIdImg2')))

    # 8 只有页眉校徽、正文无图：不该要求补图题
    body = p('一、实验目的', style='Heading1')
    body += p('纯文字，没有插图。', size=24, chars=2, line=360)
    write('header-logo.docx', build(body, header_image=True))

    # 9 WPS 风格：只有缇值缩进（480 缇 / 12pt = 2.0 字符）
    body = p('一、实验目的', style='Heading1')
    body += p('只有缇值的首行缩进。', size=24, twips=480, line=360)
    write('wps-indent.docx', build(body))

    # 10 表格内段落（应跳过）与列表段落（应跳过缩进）
    body = p('一、实验目的', style='Heading1')
    body += p('列表项一', size=24, line=360, numpr=True)
    body += ('<w:tbl><w:tr><w:tc>' + p('表格里的字', size=21, line=240) + '</w:tc></w:tr></w:tbl>')
    body += p('正常正文。', size=24, chars=2, line=360)
    write('table-list.docx', build(body))

    # 11 页边距错（上下 2.0cm，左右 2.0cm）
    body = p('一、实验目的', style='Heading1')
    body += p('页边距不对。', size=24, chars=2, line=360)
    write('margins-bad.docx', build(body, margins=(1134, 1134, 1134, 1134)))

    # 12 标题字号错（一级标题设成小四 24，要求三号 32）
    body = ('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            '<w:r><w:rPr><w:sz w:val="24"/></w:rPr><w:t>一、实验目的</w:t></w:r></w:p>')
    body += p('正文正常。', size=24, chars=2, line=360)
    write('heading-size.docx', build(body))

    # 13 封面 + 多行目录 + 正文：bodyRange 必须把前两者排除
    #    目录故意放 3 行、且第一行文本与 startAfter 完全同词（「一、实验目的」）——
    #    只放 1 行时，正文起点恰好落在目录末尾，fixture 会掩盖「起点落进目录内部」的 bug。
    body = p('数据库实验报告', size=44, line=240)
    body += p('彭超 2411020125', size=24, line=240)
    body += p('目  录', size=32, line=240)
    body += p('一、实验目的 ........ 1', size=21, line=240)
    body += p('二、实验内容 ........ 2', size=21, line=240)
    body += p('三、实验结果 ........ 3', size=21, line=240)
    body += p('一、实验目的', style='Heading1')
    body += p('封面和目录不该被当成正文检查。', size=24, chars=2, line=360)
    body += p('参考文献', style='Heading1')
    body += p('不该被检查。', size=21, line=240)
    write('cover.docx', build(body))

    # 14 图题用 SEQ 域：连续性无法核对，应降级 info
    body = p('一、实验目的', style='Heading1')
    body += p('图 1.1 集群拓扑', chars=0, line=360, style='Caption', seq=True)
    body += p('图 1.5 域里的旧值', chars=0, line=360, style='Caption', seq=True)
    write('seq-caption.docx', build(body))

    # 15 表号断档（要求分章编号的预设下才报）
    body = p('一、实验目的', style='Heading1')
    body += p('表 1.1 集群规格', chars=0, line=360, style='Caption')
    body += p('表 1.3 节点列表', chars=0, line=360, style='Caption')
    write('table-caption.docx', build(body))

    # 16 损坏的 docx：document.xml 截断。必须报「文件坏了」并退出 2，
    #    不能冒 traceback，也不能被 CI 当成「格式不合格」（1）。
    parts = build(p('一、实验目的', style='Heading1'))
    parts['word/document.xml'] = parts['word/document.xml'][:200]
    write('broken.docx', parts)

    # 17 endAt 守卫：正文中间有一句以「参考文献」开头的普通段落（五号字，故意违规）。
    #    若 endAt 在任意段落上匹配，检查范围会在此截断，这条字号错误被静默放过、
    #    整篇输出「全部通过」。期望：仍然报出这条 error。
    body = p('一、实验目的', style='Heading1')
    body += p('正文第一段，格式正确。', size=24, chars=2, line=360)
    body += p('参考文献的数量和质量决定了报告的可信度。', size=21, line=240)
    body += p('正文最后一段，格式正确。', size=24, chars=2, line=360)
    write('endat-guard.docx', build(body))


    # 18 带编号的参考文献标题（「6 参考文献」）+ 悬挂缩进的五号条目。
    #    endAt 若只认裸词「参考文献」，整章会进正文检查范围，光参考文献就报十几条 error。
    body = p('一、实验目的', style='Heading1')
    body += p('正文正常。', size=24, chars=2, line=360)
    body += p('6 参考文献', style='Heading1')
    body += p('[1] 作者. 题名[J]. 期刊, 2023.', size=21, hang=2, line=360)
    body += p('[2] 作者二. 题名[M]. 出版社, 2022.', size=21, hang=2, line=360)
    body += p('[3] 作者三. 题名[D]. 学校, 2021.', size=21, hang=2, line=360)
    write('endat-numbered-refs.docx', build(body))

    # 19 悬挂缩进的正文段落：等价于「有缩进，只是方向相反」，不该报「没有首行缩进」
    body = p('一、实验目的', style='Heading1')
    body += p('这一段用悬挂缩进排版，字号行距都合规。', size=24, hang=2, line=360)
    write('hanging-indent.docx', build(body))

    # 20 图题编号口径混用：「图 1」「图 2」与「图 1.1」同时出现。
    #    any 口径把两支分开查，混用永远不会报 —— 这是 any 唯一的真盲区。
    body = p('一、实验目的', style='Heading1')
    body += p('图 1 集群拓扑', chars=0, line=360, style='Caption')
    body += p('图 2 节点分布', chars=0, line=360, style='Caption')
    body += p('图 1.1 shell 输出', chars=0, line=360, style='Caption')
    write('mixed-captions.docx', build(body))


    # 21 学位论文：封面 + 两行目录 + 「第一章 绪论」开头（不在 startAfter 关键词里）
    #    关键词没命中时不能退化为全篇 —— 否则封面和目录重新进检查范围，稳定报十几条。
    body = p('基于 HBase 的课程设计', size=44, line=240)
    body += p('彭超 2411020125', size=24, line=240)
    body += p('目  录', size=32, line=240)
    body += p('第一章 绪论 ........ 1', size=21, line=240)
    body += p('第二章 设计 ........ 5', size=21, line=240)
    body += p('第一章 绪论', style='Heading1')
    body += p('正文段落格式完全正确。', size=24, chars=2, line=360)
    body += p('参考文献', style='Heading1')
    body += p('[1] 作者. 题名[J]. 期刊, 2023.', size=21, hang=2, line=360)
    write('cover-xulun.docx', build(body))

    # 22 起点关键词完全没命中：「第一章 概述」既不是摘要也不是绪论/引言/前言。
    #    走退化分支 —— 从第一个标题之后开始，封面与目录仍被排除。
    #    这一条守的是「退化 ≠ 全篇」：任何没被列进正则的文档都走这条路。
    body = p('课程设计报告', size=44, line=240)
    body += p('彭超 2411020125', size=24, line=240)
    body += p('目  录', size=32, line=240)
    body += p('第一章 概述 ........ 1', size=21, line=240)
    body += p('第二章 实现 ........ 4', size=21, line=240)
    body += p('第一章 概述', style='Heading1')
    body += p('正文段落格式完全正确。', size=24, chars=2, line=360)
    write('cover-fallback.docx', build(body))

    # 23 两节文档：第 1 节左边距 2cm（不合规），第 2 节 3cm（合规）。
    #    这是 §6.5 声称的「逐节核对」唯一的用例 —— 之前 20 个 fixture 全是单节。
    body = p('一、实验目的', style='Heading1')
    body += p('第一节的正文。', size=24, chars=2, line=360)
    body += sect_break(margins=(1440, 1440, 1134, 1134))   # 2cm
    body += p('第二节的正文。', size=24, chars=2, line=360)
    write('two-sections.docx', build(body))


    # 24 三级图号（图 2.3.1 / 图 2.3.2 / 图 2.4.1）：只认两级时会被判成断档
    body = p('一、实验目的', style='Heading1')
    body += p('图 2.3.1 创建名字空间', chars=0, line=360, style='Caption')
    body += p('图 2.3.2 空间列表', chars=0, line=360, style='Caption')
    body += p('图 2.4.1 创建表', chars=0, line=360, style='Caption')
    body += p('图 2.4.2 查看表', chars=0, line=360, style='Caption')
    write('caption-3level.docx', build(body))

    # 25 代码块：等宽字体、无首行缩进、固定值行距。
    #    对它套正文规则会稳定报「没有首行缩进」「字体不是宋体」「行距不是 1.5 倍」。
    body = p('一、实验目的', style='Heading1')
    body += p('执行以下命令：', size=24, chars=2, line=360)
    body += p("create 'PC:player_team','info'", size=21, line=400, rule='exact', mono='Consolas')
    body += p("put 'PC:player','0F-LAL-01','info:name','Lakers'", size=21, line=400, rule='exact', mono='Consolas')
    body += p('执行结果如上。', size=24, chars=2, line=360)
    write('code-block.docx', build(body))


if __name__ == '__main__':
    main()
