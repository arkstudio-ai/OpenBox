import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"

const [html, script, config, nginx] = await Promise.all([
  readFile(new URL("../public/landing.html", import.meta.url), "utf8"),
  readFile(new URL("../public/landing.js", import.meta.url), "utf8"),
  readFile(new URL("../public/landing.config.js", import.meta.url), "utf8"),
  readFile(new URL("../nginx.conf", import.meta.url), "utf8"),
])

assert.equal((html.match(/data-config-link="ctaUrl"/g) || []).length, 3)
assert.ok(script.includes('"nav.cta": "立刻开始"'))
assert.ok(script.includes('"hero.primaryCta": "立刻开始"'))
assert.ok(script.includes('"final.cta": "立刻开始"'))
assert.ok(script.includes('"nav.cta": "Start now"'))
assert.ok(script.includes('"hero.primaryCta": "Start now"'))
assert.ok(script.includes('"final.cta": "Start now"'))
assert.ok(!html.includes("hero.metricThreeValue"))
assert.ok(!script.includes("多语言"))
assert.ok(!script.includes("Bilingual"))
assert.ok(config.includes('ctaUrl: "https://ai.bossipai.com.cn/"'))
assert.ok(script.includes('ctaUrl: "https://ai.bossipai.com.cn/"'))
assert.ok(nginx.includes("location = /landing"))

console.log("SEO landing page checks passed")
