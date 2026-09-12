import { useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { Spinner } from "@/shared/ui/Spinner"
import { useRecordSearch } from "../../api/queries"
import type { Seq } from "../../types/protocol"
import { lteSeq } from "../../utils/seq"
import type { ViewRecord } from "../../utils/view"
import { KindBadge } from "./KindBadge"

type Scope = "position" | "latest"

interface SearchPanelProps {
  sessionId: string
  /** The shown position. */
  seq: Seq
  /** The loaded live head, for an explicit "search newer activity" scope during replay. */
  liveSeq: Seq
  live: boolean
  records: Readonly<Record<string, ViewRecord>>
  onSelect: (recordId: string) => void
  onMove: (seq: Seq, recordId: string) => void
}

/**
 * Full-range server search over the recording up to a fixed position. During
 * replay it searches the replay position unless the viewer explicitly widens
 * it to the latest activity; a hit from after the position can only be opened
 * by moving the playhead there, never expanded inside the historical view.
 */
export function SearchPanel({ sessionId, seq, liveSeq, live, records, onSelect, onMove }: SearchPanelProps) {
  const { t } = useTranslation("admin-trajectories")
  const [draft, setDraft] = useState("")
  const [query, setQuery] = useState("")
  const [scope, setScope] = useState<Scope>("position")
  const through = live || scope === "position" ? seq : liveSeq
  const search = useRecordSearch(sessionId, through, query)
  const hits = search.data?.pages.flatMap((page) => page.items) ?? []
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setQuery(draft.trim())
  }
  return (
    <section
      aria-label={t("search.title")}
      className="border-hair bg-surface flex max-h-72 flex-col gap-2 overflow-auto border-b px-3 py-2"
      data-testid="trajectory-search"
    >
      <form role="search" onSubmit={submit} className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={t("search.placeholder")}
          aria-label={t("search.placeholder")}
          className="border-hair bg-bg text-ink placeholder:text-n500 min-w-40 flex-1 rounded-full border px-3 py-1 text-xs"
          data-testid="trajectory-search-input"
        />
        <button
          type="submit"
          className="bg-ink text-bg rounded-full px-3 py-1 text-xs"
          disabled={!draft.trim()}
        >
          {t("search.submit")}
        </button>
        {!live && (
          <fieldset className="text-n700 flex items-center gap-2 text-xs">
            <legend className="sr-only">{t("search.scope")}</legend>
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name="trajectory-search-scope"
                checked={scope === "position"}
                onChange={() => setScope("position")}
              />
              {t("search.scopePosition", { seq })}
            </label>
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name="trajectory-search-scope"
                checked={scope === "latest"}
                onChange={() => setScope("latest")}
              />
              {t("search.scopeLatest", { seq: liveSeq })}
            </label>
          </fieldset>
        )}
      </form>
      {query && (
        <p className="text-n500 text-2xs" data-testid="trajectory-search-scope">
          {t("search.scopeNote", { seq: through })}
        </p>
      )}
      {query && search.isLoading && <Spinner className="size-4" />}
      {query && !!search.error && (
        <p role="alert" className="text-dangerink text-xs">
          {t("search.failed")}
        </p>
      )}
      {query && search.isSuccess && !hits.length && <p className="text-n500 text-xs">{t("search.empty")}</p>}
      <ul className="flex flex-col gap-1">
        {hits.map((hit) => {
          const reachable = !!records[hit.record_id] && lteSeq(hit.seq, seq)
          return (
            <li
              key={`${hit.record_id}-${hit.seq}`}
              className="flex min-w-0 items-center gap-2 text-xs"
              data-testid="trajectory-search-hit"
            >
              <KindBadge kind={hit.kind} className="w-20" />
              <span className="text-n500 text-2xs w-16 flex-none font-mono">
                {t("events.seq", { seq: hit.seq })}
              </span>
              {reachable ? (
                <button
                  type="button"
                  onClick={() => onSelect(hit.record_id)}
                  className="text-ink min-w-0 flex-1 truncate text-start hover:underline"
                >
                  {hit.preview ?? hit.record_id}
                </button>
              ) : (
                <>
                  <span className="text-n600 min-w-0 flex-1 truncate">{hit.preview ?? hit.record_id}</span>
                  <button
                    type="button"
                    onClick={() => onMove(hit.seq, hit.record_id)}
                    className="text-a700 flex-none hover:underline"
                  >
                    {t("search.moveTo", { seq: hit.seq })}
                  </button>
                </>
              )}
            </li>
          )
        })}
      </ul>
      {search.hasNextPage && (
        <button
          type="button"
          className="text-a700 w-fit text-xs hover:underline"
          onClick={() => void search.fetchNextPage()}
          disabled={search.isFetchingNextPage}
        >
          {t("search.more")}
        </button>
      )}
    </section>
  )
}
