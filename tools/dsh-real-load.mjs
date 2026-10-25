/**
 * 用 DSH 自带的官方 cordis 运行时真实加载 paper-format-check 一次。
 *
 * 为什么需要它：`selftest` 验的是引擎（Python）与断言，验不到「插件能不能被 dsh 装起来」。
 * 三处 name/id 一致（package.json / cordis.patch.yml / lib/index.js 的 PROVIDER_NAME）
 * 写错就开不了机，而写错时 `node -e "import(...)"` 也不报错 —— 只有真跑一遍注册表才知道。
 *
 * **不进 CI**：它依赖本机装着的 DSH Desktop（要读 resources/app 里的官方包），
 * 需要时手动跑一次。定位与 publish-size-check 的同名脚本一致。
 *
 * 用法：
 *   node tools/dsh-real-load.mjs                                  # 用默认路径
 *   node tools/dsh-real-load.mjs <包目录> <DSH app 目录>
 *
 * 默认 app 目录取 Windows 安装位置，其他平台传第二个参数。
 */
import { join, resolve } from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { existsSync } from 'node:fs'

const PKG = resolve(process.argv[2] ?? join(import.meta.dirname, '..'))
const APP = resolve(
  process.argv[3] ?? 'C:/Program Files/DSH Desktop/resources/app',
)

if (!existsSync(APP)) {
  console.error(`找不到 DSH app 目录：${APP}`)
  console.error('用法: node tools/dsh-real-load.mjs <包目录> <DSH app 目录>')
  process.exit(2)
}

const req = createRequire(join(APP, '__resolve__.cjs'))
const resolvePkg = (name) => pathToFileURL(req.resolve(name)).href

let Context
let SkillRegistry
let renderSkillContent
try {
  ;({ Context } = await import(resolvePkg('@deepseek-ai/cordis')))
  const skillMod = await import(resolvePkg('@deepseek-ai/dsh-skill'))
  SkillRegistry = skillMod.default ?? skillMod.SkillRegistry
  renderSkillContent = skillMod.renderSkillContent
  if (!SkillRegistry) {
    console.log('dsh-skill 导出:', Object.keys(skillMod))
    throw new Error('找不到 SkillRegistry 导出')
  }
} catch (e) {
  console.error('加载官方运行时失败:', String(e?.message ?? e))
  process.exit(2)
}

const mod = await import(pathToFileURL(join(PKG, 'lib', 'index.js')).href)

let fail = 0
const ok = (cond, msg) => {
  console.log((cond ? '  OK   ' : '  FAIL ') + msg)
  if (!cond) fail += 1
}

const ctx = new Context()
await ctx.plugin(SkillRegistry)
await ctx.plugin(mod)

ok(typeof ctx.skills === 'object' && ctx.skills !== null, 'ctx.skills 服务已就绪')
ok(typeof ctx.skills.registerProvider === 'function', 'ctx.skills 暴露了 registerProvider')

const catalog = await ctx.skills.list({ cwd: PKG })
const all = catalog?.skills ?? catalog
const names = (all ?? []).map((s) => s.name)
console.log('  注册表里可见的技能:', JSON.stringify(names))
ok(names.includes('paper-format-check'), '注册表里能找到 paper-format-check')

const summary = (all ?? []).find((s) => s.name === 'paper-format-check')
ok(!!summary, '拿得到摘要')
if (summary) {
  ok(summary.provider === 'paper-format-check', `provider = ${summary.provider}`)
  ok(summary.source === 'bundled', `source = ${summary.source}`)
  ok(summary.invocation?.modelInvocable === true, 'modelInvocable 为真')
  ok(summary.invocation?.userInvocable === true, 'userInvocable 为真')
  ok(summary.resourceBase?.kind === 'directory', `resourceBase.kind = ${summary.resourceBase?.kind}`)
  ok(summary.resourceBase?.path === PKG, `resourceBase.path = ${summary.resourceBase?.path}`)
  console.log('  description 前 80 字:', String(summary.description).slice(0, 80))
}

const def = await ctx.skills.get('paper-format-check', { cwd: PKG })
ok(!!def, 'ctx.skills.get() 取到技能定义')
if (def) {
  ok(typeof def.content === 'string' && def.content.length > 1000, `正文长度 ${def.content?.length}`)
  ok(!def.content.includes('{skill_base}'), '正文不含 {skill_base} 残留')
  const rendered = renderSkillContent(def)
  ok(rendered.includes('<skill_content'), '渲染出 <skill_content> 块')
  ok(rendered.includes('Base directory for this skill'), '渲染结果里带基准目录说明')
  ok(rendered.includes(PKG), '基准目录就是包根')
  ok(!rendered.includes('{skill_base}'), '渲染结果里没有占位符残留')
}

await ctx.stop?.()
console.log(fail ? `\n失败 ${fail} 项` : '\n真实加载：全部通过')
process.exit(fail ? 1 : 0)
