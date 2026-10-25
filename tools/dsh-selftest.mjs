#!/usr/bin/env node
/**
 * paper-format-check 的自测入口。
 *
 * 形态对齐 publish-size-check 的 `node tools/dsh-selftest.mjs`：不为测试再引一条 Python 入口，
 * Python 引擎由这里 spawnSync 驱动。
 *
 * 断言只有三件：退出码、summary 计数、checked 列表。
 * **不断言文案原文** —— 文案会改，断言文案会让每次改措辞都得改测试，最后没人愿意动文案。
 *
 * 另外顺手校验 SKILL.md / README.md 里引用的相对路径真实存在
 * （publish-size-check 踩过 `files` 字段漏文件的坑）。
 */

import { spawnSync } from 'node:child_process'
import {
  closeSync,
  existsSync,
  mkdtempSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const CHECK = join(ROOT, 'scripts', 'check.py')
const FIXTURES = join(ROOT, 'tests', 'fixtures')
const EXPECTATIONS = join(ROOT, 'tests', 'expectations.json')

let failures = 0
let passes = 0

function report(ok, label, detail) {
  if (ok) {
    passes += 1
    console.log(`  ok   ${label}`)
  } else {
    failures += 1
    console.log(`  FAIL ${label}${detail ? `\n       ${detail}` : ''}`)
  }
}

// stdio 走文件描述符，不用默认的 pipe。
// DSH 沙箱不允许子进程开命名管道，`spawnSync(..., {encoding:'utf8'})` 会 EPERM ——
// 而这个插件最终就是要跑在 DSH 里，自测在沙箱里跑不起来会一直硌人。
const IO_DIR = mkdtempSync(join(tmpdir(), 'pfc-io-'))

function runCheck(args, tag = 'run') {
  const outPath = join(IO_DIR, `${tag}.out`)
  const errPath = join(IO_DIR, `${tag}.err`)
  const fo = openSync(outPath, 'w')
  const fe = openSync(errPath, 'w')
  let res
  try {
    res = spawnSync('python', [CHECK, ...args], { stdio: ['ignore', fo, fe] })
  } finally {
    closeSync(fo)
    closeSync(fe)
  }
  if (res.error) {
    throw new Error(`无法执行 python：${res.error.message}`)
  }
  return {
    status: res.status,
    stdout: readFileSync(outPath, 'utf8'),
    stderr: readFileSync(errPath, 'utf8'),
  }
}

function parseJson(stdout) {
  try {
    return JSON.parse(stdout)
  } catch {
    return null
  }
}

function assertCase(spec, rulesDir) {
  const label = `${spec.file}${spec.preset ? ` --preset ${spec.preset}` : ''}${spec.rules ? ' (自定义规则)' : ''}`
  const args = [join(FIXTURES, spec.file), '--json']
  if (spec.preset) args.push('--preset', spec.preset)
  if (spec.rules) args.push('--rules', join(rulesDir, `${spec.rules}.json`))
  if (spec.extra) args.push(...spec.extra)

  const { status, stdout, stderr } = runCheck(args, label.replace(/[^\w.-]+/g, '_').slice(0, 60))

  if (spec.exit !== undefined) {
    report(status === spec.exit, `${label} 退出码 ${spec.exit}`, `实际 ${status}｜stderr: ${stderr.trim()}`)
  }

  // exit 2 的样本读不出 JSON，到退出码为止
  if (spec.exit === 2) return

  const data = parseJson(stdout)
  if (!data) {
    report(false, `${label} 输出可解析`, stdout.slice(0, 200))
    return
  }

  // 契约不变量：聚合不能让「位置」或「计数」悄悄丢失
  //   sum(count) == stats.occurrences == sum(summary.values())
  //   count == samples.length（位置必须全给，不能只给 3 个）
  const totalCount = data.findings.reduce((n, f) => n + (f.count ?? 1), 0)
  const summarySum = Object.values(data.summary).reduce((a, b) => a + b, 0)
  report(
    totalCount === data.stats.occurrences && summarySum === data.stats.occurrences,
    `${label} 计数不变量`,
    `sum(count)=${totalCount} occurrences=${data.stats.occurrences} sum(summary)=${summarySum}`,
  )
  const badSamples = data.findings.filter(
    (f) => (f.count ?? 1) > 1 && (f.samples ?? []).length !== f.count,
  )
  report(
    badSamples.length === 0,
    `${label} 位置完整`,
    badSamples.map((f) => `${f.id}: count=${f.count} samples=${(f.samples ?? []).length}`).join('; '),
  )

  if (spec.error !== undefined) {
    report(data.summary.error === spec.error, `${label} error=${spec.error}`, `实际 ${data.summary.error}`)
  }
  if (spec.warn !== undefined) {
    report(data.summary.warn === spec.warn, `${label} warn=${spec.warn}`, `实际 ${data.summary.warn}`)
  }
  if (spec.passed !== undefined) {
    report(data.passed === spec.passed, `${label} passed=${spec.passed}`, `实际 ${data.passed}`)
  }
  // checked 断言是「必须包含」，不是「必须等于」：新增检查项时不该让老用例全红。
  // 要断言「一项都没跑」用 checkedEmpty。
  if (spec.checked) {
    const actual = data.stats.checked
    const missing = spec.checked.filter((k) => !actual.includes(k))
    report(missing.length === 0, `${label} checked 含 [${spec.checked.join(',')}]`, `缺 ${missing.join(',')}｜实际 [${actual.join(',')}]`)
  }
  if (spec.checkedEmpty) {
    report(data.stats.checked.length === 0, `${label} checked 为空`, `实际 [${data.stats.checked.join(',')}]`)
  }
  if (spec.sections !== undefined) {
    report(data.stats.sections === spec.sections, `${label} sections=${spec.sections}`, `实际 ${data.stats.sections}`)
  }
  if (spec.kinds) {
    const actualKinds = new Set(data.findings.map((f) => f.kind))
    const missing = spec.kinds.filter((k) => !actualKinds.has(k))
    report(missing.length === 0, `${label} 含 ${spec.kinds.join('/')} 类 finding`, `缺 ${missing.join(',')}`)
  }
}

function assertRealDoc() {
  // 真实文档不入库（含学号姓名）。本地把 docx 放到 tests/fixtures/real/ 下即可参与回归，
  // 否则跳过 —— 但跳过要说出来，不能静默显示「全绿」让人以为跑过了。
  const doc = join(ROOT, 'tests', 'fixtures', 'real', 'HBase-shell.docx')
  const snapPath = join(ROOT, 'tests', 'fixtures', 'real', 'HBase-shell.snapshot.json')
  if (!existsSync(doc)) {
    console.log('  skip  真实文档未放置（tests/fixtures/real/HBase-shell.docx）')
    return
  }
  const snap = JSON.parse(readFileSync(snapPath, 'utf8'))
  const { status, stdout, stderr } = runCheck(
    [doc, '--json', '--preset', snap.preset, '--student-id', '2411020125', '--student-name', '彭超'],
    'real-hbase',
  )
  const data = parseJson(stdout)
  if (!data) {
    report(false, '真实文档输出可解析', stderr.slice(0, 200))
    return
  }
  report(data.stats.occurrences === snap.stats.occurrences,
    `真实文档 occurrences=${snap.stats.occurrences}`, `实际 ${data.stats.occurrences}`)
  report(JSON.stringify(data.summary) === JSON.stringify(snap.summary),
    `真实文档 summary=${JSON.stringify(snap.summary)}`, `实际 ${JSON.stringify(data.summary)}`)
  report(status === (snap.summary.error > 0 ? 1 : 0), '真实文档退出码', `实际 ${status}`)
}

function assertPackageFiles() {
  // npm pack 会静默丢掉不存在的 files 条目，所以这个错不会在打包时暴露 ——
  // 必须自己查。v0.5 就漏过：files 列了 LICENSE，仓库里却没有，
  // 而校验脚本当时把 LICENSE 显式排除在检查之外，等于给自己开了后门。
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'))
  for (const entry of pkg.files ?? []) {
    report(existsSync(join(ROOT, entry)), `files 里的 ${entry} 存在`)
  }
}

function assertRelativePaths() {
  // SKILL.md 与 README.md 里提到的相对路径必须真实存在。
  // 文档本身缺失也要报 fail —— 否则这一节在骨架落地前是空转的，
  // 「58 条全绿」会让人误以为引用完整性也被检查过了。
  for (const doc of ['SKILL.md', 'README.md']) {
    const path = join(ROOT, doc)
    report(existsSync(path), `${doc} 存在`)
    if (!existsSync(path)) continue
    const text = readFileSync(path, 'utf8')
    const refs = [...text.matchAll(/(?:python\s+)?(scripts\/[\w./-]+\.py|references\/[\w./-]+\.(?:json|md))/g)]
      .map((m) => m[1])
    for (const ref of new Set(refs)) {
      report(existsSync(join(ROOT, ref)), `${doc} 引用的 ${ref} 存在`)
    }
  }
}

function main() {
  console.log('paper-format-check selftest')
  console.log(`  root: ${ROOT}`)

  const { cases, special } = JSON.parse(readFileSync(EXPECTATIONS, 'utf8'))

  console.log('\n[fixtures]')
  for (const spec of cases) assertCase(spec, null)

  console.log('\n[特殊用例]')
  const tmp = mkdtempSync(join(tmpdir(), 'pfc-'))
  try {
    writeFileSync(
      join(tmp, 'nulls.json'),
      JSON.stringify({
        version: 3,
        defaults: {},
        presets: {
          空: {
            body: { size: null, firstLineChars: null, lineSpacing: null, eastAsia: null },
            captions: { scheme: null, requireFigureCaption: null },
            headings: { 1: { size: null, bold: null } },
          },
        },
      }),
      'utf8',
    )
    for (const spec of special) assertCase(spec, tmp)
  } finally {
    rmSync(tmp, { recursive: true, force: true })
  }

  console.log('\n[真实文档（可选）]')
  assertRealDoc()

  console.log('\n[包内容]')
  assertPackageFiles()

  console.log('\n[引用完整性]')
  assertRelativePaths()

  rmSync(IO_DIR, { recursive: true, force: true })
  console.log(`\n通过 ${passes}，失败 ${failures}`)
  process.exitCode = failures === 0 ? 0 : 1
}

main()
