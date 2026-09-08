// Money display. The server keeps amounts as integer fen and credits as
// Decimal strings; this module is the single place where a fen integer becomes
// something a person reads.
//
// Rule: exactly one arithmetic operation on money — `amountFen / 100`. No
// summing, averaging or rounding of fen in JS, because IEEE doubles turn a
// column of totals into a reconciliation dispute. Anything aggregated is
// aggregated by the server and arrives as a string.

/** Falls back to `<code> <number>` when the ISO code is one Intl rejects. */
function currencyFormat(currency: string, locale: string): Intl.NumberFormat | null {
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  } catch {
    return null
  }
}

/**
 * `formatFen(999, "CNY", "zh-CN")` → `"¥9.99"`.
 *
 * Always two decimals: a price that renders as "¥9.9" reads as a rounding bug
 * to anyone reconciling it against the provider's console.
 */
export function formatFen(amountFen: number, currency: string, locale: string): string {
  const value = amountFen / 100
  const formatter = currencyFormat(currency, locale)
  if (formatter) return formatter.format(value)
  const plain = new Intl.NumberFormat(locale, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
  return `${currency} ${plain}`
}
