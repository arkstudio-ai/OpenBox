function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`)
      .join(",")}}`
  }
  return JSON.stringify(value) ?? "undefined"
}

/** Compare every persisted field, including added/removed optional fields. */
export function versionChanges(before: object, after: object) {
  const old = before as Record<string, unknown>
  const next = after as Record<string, unknown>
  return [...new Set([...Object.keys(old), ...Object.keys(next)])]
    .filter((key) => canonical(old[key]) !== canonical(next[key]))
    .map((key) => ({ key, before: old[key], after: next[key] }))
}
