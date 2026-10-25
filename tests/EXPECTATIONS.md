# fixture 期望结果

机器可读的版本在 `expectations.json`，由 `node tools/dsh-selftest.mjs` 驱动。
这份文件是给人看的索引：每个 fixture 想证明什么、期望什么结论。

断言只有三件：退出码、summary 计数、checked 列表。**不断言文案原文** —— 文案会改。

---

## 正常路径

**clean.docx** —— 全合规基准，防误报
预设「实验报告」；退出 0；error 0 / warn 0；checked 应含全部七项

**cover.docx** —— 封面 + 三行目录 + 正文 + 参考文献，验证 `bodyRange`
退出 0；error 0 / warn 0；checked 含 `bodyRange`
目录故意放三行，且第一行与 `startAfter` 同词（「一、实验目的」）。只放一行时正文起点会恰好落在目录末尾，掩盖「起点落进目录内部」的 bug —— 这个 fixture 曾经就是这么骗过一轮的。

**header-logo.docx** —— 页眉有校徽、正文无图
退出 0；warn 0。证明图片计数没把页眉 logo 算成正文图片。

**wps-indent.docx** —— 只有缇值的首行缩进（480 缇 / 12pt = 2.0 字符）
退出 0；warn 0。WPS 导出的典型形态，不该误报。

**table-list.docx** —— 表格内段落 + 列表段落
退出 0；warn 0。证明两类段落被正确跳过。

**seq-caption.docx** —— 图题用 SEQ 域，且域值是旧的（1.1 之后是 1.5）
退出 0；warn 0。证明降级为 info 而不是报断档。

## 降级边界（成对存在，只测一个证明不了什么）

**grid.docx** —— docGrid 312，行距也是 312
退出 0；warn 0。网格能解释，降级 info。

**grid-mismatch.docx** —— docGrid 312，行距却是固定值 400
退出 0；warn 1。网格解释不了，仍按固定值行距报 warn。
这两个必须成对：只测前者，会把「一律降级」当成正确实现。

**fixed-line.docx** —— 没有网格，行距是固定值 400
退出 0；warn 1。真阳性。

## 真阳性

**bad.docx** —— 字号、缩进、行距、页眉、图号五类同时违规
退出 1；error 7 / warn 1；含 `header` / `body` / `captions` 三类 finding

**margins-bad.docx** —— 四边页边距全部 2.0cm
退出 1；error 4；含 `margins`

**heading-size.docx** —— 一级标题设成小四，要求三号
退出 0；warn 1；含 `headings`
标题字号是 `warn` 不是 `error`：它的正确值取决于预设，而预设只是猜测。要阻塞交给 `--strict`。

**table-caption.docx** —— 表 1.1 之后直接表 1.3
退出 1；error 1；含 `captions`

**nocaption.docx** —— 正文两张图，没有任何图题
退出 0；warn 1；含 `captions`

**breaks.docx** —— 一处 Shift+Enter + 三个连续空段
退出 0；warn 2；含 `breaks`

## 读不了的文件

**cover-xulun.docx** —— 封面 + 两行目录 + 「第一章 绪论」开头（学位论文预设）
退出 0；error 0。守 `startAfter` 认「绪论」。

**cover-fallback.docx** —— 「第一章 概述」开头，不在任何关键词里
退出 0；error 0；含 `bodyRange`。
守的是「退化 ≠ 全篇」：关键词没命中时从第一个标题之后开始，封面与目录仍被排除。

**two-sections.docx** —— 第 1 节左边距 2cm、第 2 节 3cm
退出 1；error 2；含 `margins`；`sections == 2`。
只报第 1 节的两条，第 2 节不报 —— §6.5 的「逐节核对」此前没有任何用例。

**caption-3level.docx** —— 图 2.3.1 / 2.3.2 / 2.4.1 / 2.4.2（三级编号）
退出 0；error 0。只认两级时这四条会被全判成断档 —— 首份真实语料里 45 条 error 就是这么来的。

**code-block.docx** —— 等宽字体（Consolas）的命令块，无缩进、固定值行距
退出 0；error 0。代码块整段跳过，不套正文规则。

**broken.docx** —— `document.xml` 被截断
退出 2，不输出 JSON。必须报「文件已损坏」而不是 traceback，也不能被 CI 当成「格式不合格」（1）。
半下载、WPS 崩溃写坏的 docx 都是这个形态。

**endat-numbered-refs.docx** —— 「6 参考文献」+ 三条悬挂缩进的五号文献
退出 0；error 0。
一个样本守两个独立误报源：`endAt` 只认裸词「参考文献」会把整章算进正文（报十几条字号/缩进）；`w:ind/@w:hanging` 没读会把悬挂缩进当成「没有首行缩进」。

**hanging-indent.docx** —— 正文段落用悬挂缩进，字号行距合规
退出 0；error 0。悬挂缩进等价于「有缩进，只是方向相反」。

**mixed-captions.docx** —— 「图 1」「图 2」与「图 1.1」同篇
退出 0；error 0；含 `captions`。`any` 口径下两支分别查，混用本身报一条 info。

**endat-guard.docx** —— 正文中间一句以「参考文献」开头的普通段落（五号字，故意违规）
退出 1；error 3；含 `body`。
守的是「检查范围被悄悄截短，然后输出全部通过」——`endAt` 若在任意段落上匹配，这句会被当成参考文献的起点，后面的检查全部跳过。比误报更坏的一类失败。

## 特殊用例（规则文件层面）

**全 null 预设** —— `body` / `captions` / `headings` 的键全为 null
退出 1；`checked` 为空。这是 §7.6 的靶子：检查器会被调用，但一次比较都不做。

**`--strict` + 只有 warn 的文档**（fixed-line.docx）
退出 1；`passed: false`。JSON 与退出码必须一致 —— 否则读 JSON 的判合格、读退出码的判不合格。

---

## 增补 fixture 的规矩

1. 在 `make-fixtures.py` 里写清「这个样本想测什么」，注释里说明参数为什么取这个值；
2. 在 `expectations.json` 加一条，断言退出码 + summary + 必要的 checked；
3. 若测的是「边界」，必须成对存在（能解释 vs 不能解释、该降级 vs 该报错）；
4. 跑 `node tools/dsh-selftest.mjs`，必须全绿再提交。
