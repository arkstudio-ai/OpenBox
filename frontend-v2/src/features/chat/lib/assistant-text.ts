// Older personal-assistant answers were written before it learned to keep
// internal identifiers to itself ("（ID: 01M4…）", "（outcome: succeeded）").
// The saved history stays as it is; the page just does not show those bits.
// Only identifier-shaped tokens and known internal field names are touched,
// so ordinary parentheses and code in an answer are left alone.

/** A ULID (Crockford base32) or a prefixed platform id such as session_7YBV…. */
const ID = String.raw`(?:[0-9A-HJKMNP-TV-Z]{26}|(?:session|task|result|memory|mem|msg|message|run|inbox|command|request|briefing|part|cmd)_[0-9A-Za-z]{12,})`
/** Field names the platform's tools return; never words a person writes. */
const FIELD = String.raw`(?:outcome|enabled|status|state|observed_state|desired_state|delivery_state|task_id|session_id|result_id|run_id|message_id|request_id|inbox_id|command_id|revision|generation|report_attempt)`

const LABELLED_ID = new RegExp(String.raw`\s*[（(]\s*(?:[一-龥A-Za-z ]{0,8}\s*)?(?:ID|id|编号)\s*[:：]\s*\x60?${ID}\x60?\s*[）)]`, "g")
const FIELD_VALUE = new RegExp(String.raw`\s*[（(]\s*\x60?${FIELD}\s*[:：=]\s*[A-Za-z0-9_.\-]+\x60?\s*[）)]`, "g")
const CODE_ID = new RegExp(String.raw`\s*\x60${ID}\x60`, "g")
const BARE_LABELLED_ID = new RegExp(String.raw`[，,]?\s*(?:[\u4e00-\u9fa5]{0,4}|[A-Za-z]{0,8}\s?)(?:ID|编号)\s*[:：]\s*${ID}`, "g")

export function hideInternalIds(text: string): string {
  if (!text) return text
  return text
    .replace(LABELLED_ID, "")
    .replace(FIELD_VALUE, "")
    .replace(CODE_ID, "")
    .replace(BARE_LABELLED_ID, "")
}
