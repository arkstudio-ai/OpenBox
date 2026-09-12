// Python value semantics the server projector relies on. Parity with
// backend/trajectory/projector.py depends on these small details: `a or b`
// treats "", 0, [] and {} as false; `dict.get(k, d)` only falls back when the
// key is absent; `json.dumps` spacing differs between call sites; and slices
// count code points, not UTF-16 units.

export type PlainObject = Record<string, unknown>

export function isPlainObject(value: unknown): value is PlainObject {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

export function pyTruthy(value: unknown): boolean {
  if (value === null || value === undefined || value === false || value === 0 || value === "") return false
  if (Array.isArray(value)) return value.length > 0
  if (isPlainObject(value)) return Object.keys(value).length > 0
  return true
}

/** Python `a or b or c`: the first truthy operand, else the last one. */
export function pyOr(...values: unknown[]): unknown {
  for (const value of values) if (pyTruthy(value)) return value
  return values.length ? values[values.length - 1] : undefined
}

/** Python `dict.get(key, default)`. JSON null is a present value, not a fallback. */
export function pyGet(source: PlainObject, key: string, fallback: unknown = null): unknown {
  return Object.prototype.hasOwnProperty.call(source, key) ? source[key] : fallback
}

/** `json.dumps(value, ensure_ascii=False)`; `compact` selects `separators=(",", ":")`. */
export function pyDumps(value: unknown, compact = false): string {
  const itemSep = compact ? "," : ", "
  const keySep = compact ? ":" : ": "
  if (value === null || value === undefined) return "null"
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return JSON.stringify(value)
  }
  if (Array.isArray(value)) return `[${value.map((item) => pyDumps(item, compact)).join(itemSep)}]`
  if (isPlainObject(value)) {
    const entries = Object.entries(value).map(
      ([key, item]) => `${JSON.stringify(key)}${keySep}${pyDumps(item, compact)}`,
    )
    return `{${entries.join(itemSep)}}`
  }
  return JSON.stringify(String(value))
}

/** Python `==` on JSON values: dict equality ignores key order. */
export function pyEqual(a: unknown, b: unknown): boolean {
  if (a === undefined) a = null
  if (b === undefined) b = null
  if (a === b) return true
  if (Array.isArray(a) || Array.isArray(b)) {
    return (
      Array.isArray(a) &&
      Array.isArray(b) &&
      a.length === b.length &&
      a.every((item, index) => pyEqual(item, b[index]))
    )
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const keys = Object.keys(a)
    return keys.length === Object.keys(b).length && keys.every((key) => key in b && pyEqual(a[key], b[key]))
  }
  return false
}

/** Python `str(value)` for the JSON values a projector can see. */
export function pyStr(value: unknown): string {
  if (value === null || value === undefined) return "None"
  if (typeof value === "string") return value
  if (typeof value === "boolean") return value ? "True" : "False"
  if (typeof value === "number") return String(value)
  return pyDumps(value)
}

/** `text[:limit]` by code point. */
export function sliceCodePoints(text: string, limit: number): string {
  if (text.length <= limit) return text
  let count = 0
  let index = 0
  while (index < text.length && count < limit) {
    const code = text.codePointAt(index) ?? 0
    index += code > 0xffff ? 2 : 1
    count += 1
  }
  return text.slice(0, index)
}

/** projector._preview: strings as-is, other values as compact JSON, then clipped. */
export function pyPreview(value: unknown, limit = 240): string | null {
  if (value === null || value === undefined) return null
  const text = typeof value === "string" ? value : pyDumps(value, true)
  return sliceCodePoints(text, limit)
}

const ISO = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})?$/

/**
 * Epoch microseconds for an ISO timestamp, keeping the six fractional digits
 * `datetime.fromisoformat` keeps. Date() alone would truncate to milliseconds.
 */
export function isoMicros(value: string): number | null {
  const match = ISO.exec(value)
  if (!match) return null
  const [, y, mo, d, h, mi, s, fraction = "", zone = "Z"] = match
  const millis = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s))
  let offsetMinutes = 0
  if (zone !== "Z") {
    const sign = zone.startsWith("-") ? -1 : 1
    offsetMinutes = sign * (Number(zone.slice(1, 3)) * 60 + Number(zone.slice(4, 6)))
  }
  return (millis - offsetMinutes * 60_000) * 1000 + Number(fraction.padEnd(6, "0"))
}

/** `max(0, (end - start).total_seconds() * 1000)`, with the same float steps. */
export function elapsedMs(start: string, end: string): number | null {
  const from = isoMicros(start)
  const to = isoMicros(end)
  if (from === null || to === null) return null
  return Math.max(0, ((to - from) / 1e6) * 1000)
}
