# paper-format-check

发布前查图，提交前查文档 —— 这是同一家族的第二件工具。

论文 / 实验报告的格式体检。给定一份 `.docx` 和一套格式规则，输出**可定位、可执行**的问题清单。

零依赖（纯 Python 标准库，不需要 python-docx）、原件只读、判定口径保守。

引擎分三层：`scripts/ooxml.py` 解析 docx、`scripts/checks.py` 判定、`scripts/check.py` 编排与 CLI —— 加规则不必碰 XML 解析。

```console
$ python scripts/check.py 实验三_HBase\ shell\ 操作.docx --student-id 2411020125 --student-name 彭超

报告：实验三_HBase shell 操作.docx
预设：实验报告　非空段落：86　表格：2　图片：5
已查：header、margins、bodyRange、body、breaks、captions、headings

[F001] header
    页眉里找不到姓名 彭超（当前页眉：2411020125）
    修法：双击页眉 → 补上姓名 彭超

[F002] body · 二、实验内容 · 第 12 段「随着数据量的增长，HBase 采用…」
    字号 五号 10.5pt，要求 小四 12pt
    修法：选中该段 → 字号改为 小四 12pt

[F003] body · 二、实验内容 · 第 12 段「随着数据量的增长，HBase 采用…」
    行距约 1.00 倍，要求 1.5 倍
    修法：段落 → 行距 → 倍数，设为 1.5

结论：有 3 处必须修改（error 3 · warn 0 · info 0）
```

## 为什么需要它

格式检查这件事有三个特点：机械（全是可判定的客观参数）、高频（一学期十几份，每门课要求还不一样）、反馈滞后（自己查不出来，被打回来才知道）。

Word 的「显示格式」只能逐段看，不能批量比对；「格式刷」解决的是改不是查；学院发的格式要求是给人读的 PDF，机器不可读。

所以缺的是：把「格式要求」变成可执行规则、再拿规则批量扫全文的东西。

## 查什么

- 正文字号、首行缩进、行距、中文字体
- 页眉是否带学号与姓名
- 页边距（逐节）
- 图题、表题的编号连续性
- 手动换行（Shift+Enter）与连续空段落 —— 肉眼看不出来但确实错
- 标题各级字号与加粗

**不查**：内容（错别字、语句、抄袭）、自动修复、`.doc`。

## 安装

作为 DeepSeek Harness 插件：

```bash
dsh plugin --profile web add github:Stephen-pc/paper-format-check
```

包内 `package.json` 已声明 `dsh.bundle`、`lib/` 已提交，所以不需要 `allowBuilds` 授权。

独立使用（只跑检查，不装插件）：

```bash
git clone https://github.com/Stephen-pc/paper-format-check
cd paper-format-check
python scripts/check.py 你的报告.docx
```

需要 Python 3.8+，**不需要安装任何第三方包**。

## 用法

```bash
python scripts/check.py 报告.docx                        # 用「实验报告」预设
python scripts/check.py 报告.docx --preset 学位论文       # 换预设
python scripts/check.py 报告.docx --list-presets         # 看有哪些预设
python scripts/check.py 报告.docx --show-rules           # 看实际生效的规则
python scripts/check.py 报告.docx --json                 # 机器可读（永不截断）
python scripts/check.py 报告.docx --strict               # warn 也算不通过
python scripts/check.py 报告.docx --ignore margins        # 本次不查页边距
```

批量（助教批改）：

```bash
python scripts/check.py --batch 作业目录                  # 每份一行结论 + 汇总
python scripts/check.py --batch 作业目录 --fail-list      # 只要要退的路径
python scripts/check.py --batch 作业目录 --strict --fail-list
```

退出码：`0` 通过，`1` 不通过，`2` 文件读不了（不存在 / 非 docx / 加密 / XML 损坏）。

## 按自己学院的要求改规则

每个学院的格式要求都不一样，所以规则外置在 `references/rules.json`，插件本身不带「正确答案」。

复制一份，改 `presets` 里的键值，用 `--rules` 指过去：

```bash
cp references/rules.json 我的格式.json
# 编辑 我的格式.json
python scripts/check.py 报告.docx --rules 我的格式.json --preset 我的格式
```

字段含义见 `references/rules.md`（随包分发，不用去仓库外找）。三五个字段就能覆盖 80% 的要求。

## 判定口径

宁可漏，不可误报。误报一次，人就不信这个工具了；漏报只是被打回来，跟不用工具的结果一样。

具体三条：读不到就不报（报 info 不报 error）；不确定就降级（文档网格、固定值行距、SEQ 域、自动编号）；能定位才报（每条 finding 必须指到标题路径 + 段内序号）。

还有一条硬规矩：**本次真正比较过的检查项为 0 时，禁止判通过** —— 规则文件写错会让所有检查静默跳过，此时报「通过」就是在骗人。

## 自测

```bash
node tools/dsh-selftest.mjs
```

16 个合成 fixture + 2 个特殊用例，断言退出码、summary、checked 三件（不断言文案）。fixture 由 `tests/make-fixtures.py` 生成，样本规格是可 review 的源码。期望值见 `tests/EXPECTATIONS.md`。

## 已知限制

- 编号连续性、章号一致、页码域、纸张尺寸尚未实现
- 参考文献格式只做形态提示，不做判定
- 误报率尚未用真实语料验证（设计稿 §11.3 的验收口径是 ≥20 份真实文档、precision ≥95%）

## 许可

MIT
