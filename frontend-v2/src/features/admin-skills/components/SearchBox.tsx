import { useEffect, useRef, useState } from "react"
import { Search } from "lucide-react"

/**
 * How long the box waits before committing what was typed.
 *
 * Every commit is a query-key change, a request, and an `admin.view_skills`
 * audit row carrying `q` (plan §4.7) — so a live box turns "playwright" into ten
 * reads and ten audit rows of typing noise. Long enough to swallow a typed word,
 * short enough that the list still feels like it is following along.
 */
const COMMIT_DELAY = 350

interface Props {
  value: string
  onChange: (value: string) => void
  placeholder: string
  label: string
}

/** The search field shared by the store and install lists. */
export function SearchBox({ value, onChange, placeholder, label }: Props) {
  const [draft, setDraft] = useState(value)
  const [committed, setCommitted] = useState(value)

  // The committed value lives in the URL, which the back button can change
  // underneath us. Re-syncing during render rather than in an effect keeps the
  // old text from being painted for a frame first.
  if (committed !== value) {
    setCommitted(value)
    setDraft(value)
  }

  // Callers pass an inline arrow, so a fresh identity every render would re-arm
  // the timer forever. The ref keeps the latest one without being a dependency.
  const commit = useRef(onChange)
  useEffect(() => {
    commit.current = onChange
  })

  useEffect(() => {
    if (draft === value) return
    const timer = window.setTimeout(() => commit.current(draft), COMMIT_DELAY)
    return () => window.clearTimeout(timer)
  }, [draft, value])

  return (
    <div className="border-hair bg-bg flex min-w-0 flex-1 items-center gap-2 rounded-xl border px-3 py-2">
      <Search size={15} className="text-n600 flex-none" aria-hidden />
      <input
        type="search"
        value={draft}
        aria-label={label}
        onChange={(event) => setDraft(event.target.value)}
        // Enter means "I am done typing"; there is no reason to make the
        // operator wait out the delay they just outran.
        onKeyDown={(event) => {
          if (event.key === "Enter") onChange(draft)
        }}
        placeholder={placeholder}
        className="text-ink placeholder:text-n600 min-w-0 flex-1 bg-transparent text-sm outline-none"
      />
    </div>
  )
}
