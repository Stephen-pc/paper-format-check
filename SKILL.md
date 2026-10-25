---
name: paper-format-check
description: 论文 / 实验报告格式体检。给定一份 .docx，对照格式要求核验正文字号、首行缩进、行距、中文字体、页眉学号姓名、页边距、图题表题编号，输出可定位的问题与修法。只查格式参数，不做内容审查，原件只读不改。用户说「这份报告格式对不对」「能不能交」「老师要求的字号行距对不对」「页眉学号漏了没」时使用。
description_en: Format checker for theses and lab reports. Verifies body font size, first-line indent, line spacing, Chinese font, header student ID/name, page margins, and figure/table caption numbering against a rules preset, then reports locatable problems with concrete fixes. Geometry and formatting only — no content review, and the original file is never modified.
user-invocable: true
disable-model-invocation: false
---

# paper-format-check

交作业前，一条命令告诉你这份 docx 哪几处不符合格式要求，以及怎么改。

## 什么时候用

用户手上有一份 `.docx`，要交作业、投稿、交论文，但不确定格式合不合格。典型说法：

- 「这份实验报告格式对不对」「能不能交」
- 「老师要求正文小四、1.5 倍行距，你帮我看看」
- 「页眉上学号姓名有没有漏」
- 「图号是不是连着编的」

也可以作为**寄生自检**：刚生成或改完一份 docx、用户提到要交作业时，自己跑一遍，通过就一句话带过，不通过才展开。

## 怎么跑

```bash
python scripts/check.py 报告.docx
python scripts/check.py 论文.docx --preset 学位论文 --student-id 2411020125 --student-name 彭超
python scripts/check.py 报告.docx --json
```

常用参数：

- `--preset <名字>`　选预设，默认「实验报告」。`--list-presets` 看有哪些。
- `--rules <路径>`　换成另一份规则文件（必须含 `presets` 字段），之后仍要用 `--preset` 选。
- `--student-id` / `--student-name`　页眉核对的期望值。不传则退化为「有没有 8-12 位数字 / 有没有 2-4 个汉字」；姓名不传时只报 info，不假装核对过。
- `--json`　机器可读，永不截断，给 CI 用。
- `--strict`　warn 也判不通过。助教批改用。
- `--show-rules`　打印合并后实际生效的规则。怀疑「预设选错了」时先看这个。

退出码：`0` 通过，`1` 不通过，`2` 文件读不了（不存在 / 不是 docx / 加密 / XML 损坏）。

## 怎么读结果

人读输出按检查项优先序排列：页眉 → 页边距 → 正文边界 → 正文 → 手动换行 → 图题表题 → 标题。前三条是「不改就退」的，所以排最前。

每条 finding 带三样东西：定位（`一、实验目的 · 第 12 段`）、问题（含实际值与期望值）、修法（具体操作路径）。**不要转述成「格式有问题」** —— 把修法原文给用户，他能直接照着改。

`severity` 分三级：`error` 是学校一定会退回的；`warn` 是可能有问题但存在合理例外的；`info` 是提示。只转述 `error` 和 `warn`，`info` 除非用户问否则不用念。

## 边界（必须守住）

- **只查格式参数，不做内容审查。** 错别字、语句、抄袭、论证质量都不在范围内。用户问「内容写得怎么样」时明确说这不归它管。
- **只读。** 原件一个字节都不改。不要提议「帮你改好」——改格式等于替人写作业。
- **只认 `.docx`。** `.doc` 请先另存为 `.docx`。
- **读不到就不报。** 某段没显式设置字号时报 info 而不是 error，因为值可能继承自样式。不要把 info 说成错误。

## 规则怎么改

格式要求每个学院都不一样，所以规则外置在 `references/rules.json`。三套内置预设：`实验报告`、`学位论文`、`宽松版`。

要按自己学院的要求改：复制 `references/rules.json`，改 `presets` 里的键值，用 `--rules 我的格式.json --preset 我的格式` 指过去。字段含义见 `references/rules.md`（随包分发）。

## 已知限制

- 编号连续性、章号一致、页码域、纸张尺寸**尚未实现**，别向用户承诺。
- 参考文献格式只做形态提示，不做判定（GB/T 7714 变体太多）。
- 需要 **Python 3.8+**，但不需要任何第三方包。跑之前先确认 `python --version`，版本不够就明确告诉用户，不要硬跑。
- 误报率尚未做真实语料验证。用户抱怨误报时，先让他用 `--show-rules` 确认预设选对了。
