// Admin-trajectories translation audit (QA helper, not a CI gate).
// check:i18n only proves zh-CN/en-US parity; this proves the namespace matches
// what the feature actually references: every key-shaped literal resolves
// (plural bases included), nothing in the namespace is unreferenced, and
// literals from sections the namespace does not know yet are listed so newly
// authored UI keys are not silently left untranslated.
//
//   node e2e/helpers/trajectory-i18n-audit.mjs [--json out.json]
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs"
import { join, relative } from "node:path"

const root = new URL("../..", import.meta.url).pathname
const featureDir = join(root, "src/features/admin-trajectories")
const NS = "admin-trajectories"
const PLURAL = /_(zero|one|two|few|many|other)$/

const flat = (obj, prefix = "") =>
  Object.entries(obj).flatMap(([k, v]) => {
    const key = prefix ? `${prefix}.${k}` : k
    return v !== null && typeof v === "object" && !Array.isArray(v) ? flat(v, key) : [[key, v]]
  })

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return walk(path)
    return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) ? [path] : []
  })
}

const locales = Object.fromEntries(
  ["en-US", "zh-CN"].map((lang) => [
    lang,
    new Map(flat(JSON.parse(readFileSync(join(root, "src/locales", lang, `${NS}.json`), "utf8")))),
  ]),
)
const en = locales["en-US"]
const baseKeys = new Set([...en.keys()].map((key) => key.replace(PLURAL, "")))
const sections = new Set([...baseKeys].map((key) => key.split(".")[0]))

// Raw values that are identical to a key, or leak an interpolation name, are
// placeholders rather than translations.
const suspicious = []
for (const [lang, map] of Object.entries(locales)) {
  for (const [key, value] of map) {
    if (typeof value !== "string" || !value.trim()) suspicious.push({ lang, key, problem: "empty" })
    else if (value === key || baseKeys.has(value)) suspicious.push({ lang, key, problem: "value is a key" })
  }
}

// Event types (family.action) are protocol strings, not translation keys.
const protocol = readFileSync(join(featureDir, "types/protocol.ts"), "utf8")
const eventTypes = new Set()
for (const [, family, actions] of protocol.matchAll(/^\s+(\w+): \[([^\]]*)\],?$/gm)) {
  for (const [, action] of actions.matchAll(/"(\w+)"/g)) eventTypes.add(`${family}.${action}`)
}

// Watermark socket frames (shared/ws/events TrajectoryWsEventMap) are wire names too.
eventTypes.add("trajectory.available")

const referenced = new Map()
const unclassified = new Map()
const dynamicCalls = []
const foreignNamespaces = []
const hardcoded = []
const LITERAL = /"([a-z][A-Za-z0-9]*(?:\.[a-zA-Z][A-Za-z0-9_]*){1,3})"(\s*:)?/g

for (const file of walk(featureDir)) {
  const rel = relative(root, file)
  const text = readFileSync(file, "utf8")
  for (const match of text.matchAll(LITERAL)) {
    const [, literal, colon] = match
    // Object keys ("agent.input": …, "request.input": Panel) are lookup ids. A
    // ternary branch (`cond ? "a.b" : "c.d"`) is also followed by ":", so only a
    // literal that starts a property — after "{", "," or at line start — counts.
    const before = text.slice(Math.max(0, match.index - 200), match.index)
    const objectKey = colon && /(^|[{,\n])\s*$/.test(before)
    // TAB_FIELDS["interrupt.impact"] is a table lookup too.
    const indexed = /\[\s*$/.test(before)
    if (objectKey || indexed || eventTypes.has(literal)) continue
    const bucket = sections.has(literal.split(".")[0]) ? referenced : unclassified
    if (!bucket.has(literal)) bucket.set(literal, new Set())
    bucket.get(literal).add(rel)
  }
  for (const match of text.matchAll(/useTranslation\(([^)]*)\)/g)) {
    const arg = match[1].trim()
    if (arg && arg !== "NS" && arg !== `"${NS}"`) foreignNamespaces.push({ file: rel, arg })
  }
  if (rel.endsWith(".tsx")) {
    const lines = text.split("\n")
    lines.forEach((line, index) => {
      const at = `${rel}:${index + 1}`
      for (const m of line.matchAll(/\bt\(\s*([^"`\s)][^,)]*)/g)) dynamicCalls.push({ at, arg: m[1].trim() })
      for (const m of line.matchAll(
        /\b(aria-label|title|placeholder|alt|label)="([^"]*[A-Za-z一-鿿][^"]*)"/g,
      )) {
        hardcoded.push({ at, kind: `${m[1]} attribute`, text: m[2] })
      }
      // JSX text between tags that is not an expression, e.g. <span>Loading</span>.
      for (const m of line.matchAll(/>([^<>{}]*[A-Za-z一-鿿][^<>{}]*)</g)) {
        const value = m[1].trim()
        // Comparisons inside code (`i >= 0; i -= 1) if (a <`) look like tags to this regex.
        if (
          value &&
          !/^[\s#·:→—()[\],.+-]*$/.test(value) &&
          !/=>|&&|\|\||\?\s|;|[-+!=]=|\bconst\b|\breturn\b/.test(m[1])
        ) {
          hardcoded.push({ at, kind: "JSX text", text: value })
        }
      }
    })
  }
}

const missing = [...referenced.keys()].filter((key) => !baseKeys.has(key)).sort()
const referencedBases = new Set(referenced.keys())
const unused = [...baseKeys].filter((key) => !referencedBases.has(key)).sort()

const report = {
  namespace: NS,
  keysInLocale: en.size,
  baseKeys: baseKeys.size,
  referencedKeys: referenced.size,
  missing: missing.map((key) => ({ key, files: [...referenced.get(key)] })),
  unused,
  unclassifiedLiterals: [...unclassified.entries()].map(([literal, files]) => ({
    literal,
    files: [...files],
  })),
  suspiciousValues: suspicious,
  foreignNamespaces,
  dynamicCalls,
  hardcodedCandidates: hardcoded,
}

const out = process.argv.indexOf("--json")
if (out !== -1 && process.argv[out + 1])
  writeFileSync(process.argv[out + 1], `${JSON.stringify(report, null, 2)}\n`)

console.log(
  `${NS}: ${report.baseKeys} base keys (${report.keysInLocale} with plural forms); ${report.referencedKeys} referenced`,
)
console.log(
  `missing: ${missing.length}  unused: ${unused.length}  suspicious: ${suspicious.length}  unclassified literals: ${unclassified.size}`,
)
for (const item of report.missing) console.log(`  ✗ missing ${item.key}  (${item.files.join(", ")})`)
for (const key of unused) console.log(`  · unused ${key}`)
for (const item of suspicious) console.log(`  ! ${item.lang} ${item.key}: ${item.problem}`)
for (const item of report.unclassifiedLiterals)
  console.log(`  ? literal ${item.literal}  (${item.files.join(", ")})`)
for (const item of foreignNamespaces) console.log(`  ? namespace ${item.arg} in ${item.file}`)
console.log(`dynamic t() call sites: ${dynamicCalls.length}; hardcoded candidates: ${hardcoded.length}`)
if (missing.length || suspicious.length) process.exitCode = 1
