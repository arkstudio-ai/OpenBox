import { useId } from "react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { BookOpen, Pencil, Trash2 } from "lucide-react"
import type { MemoryRecord } from "@/shared/api/memory"
import { formatDay, formatSince } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import type { MemoryTopic } from "./data"
import { useBackState } from "./back"
import { Highlight } from "./Highlight"
import { card, iconButton } from "./ui"

const MAX_TOPICS = 2
const STYLE_PREFIX = "personal.style."
const DAY = 86_400_000

export interface MemoryListProps {
  memories: MemoryRecord[]
  /** Under headings by when each changed (today, yesterday, the past week, earlier): what was
   *  learned lately reads first. The list is already newest first. */
  timeline?: boolean
  query: string
  projectId: string
  busy: boolean
  topicsOf: Map<string, MemoryTopic[]>
  scopeName: (memory: MemoryRecord) => string
  onOpen: (memory: MemoryRecord) => void
  onEdit: (memory: MemoryRecord) => void
  onForget: (memory: MemoryRecord) => void
}

type Period = "today" | "yesterday" | "week" | "earlier"

function periodOf(iso: string | undefined, now = new Date()): Period {
  if (!iso) return "earlier"
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const at = new Date(iso).getTime()
  if (at >= startOfToday) return "today"
  if (at >= startOfToday - DAY) return "yesterday"
  if (at >= startOfToday - 6 * DAY) return "week"
  return "earlier"
}

/** Remembered facts as one quiet list: the sentence first, where it lives,
 *  when it changed and how it was learned second, the two things people do with it at the end. */
export function MemoryList(props: MemoryListProps) {
  const { t } = useTranslation("knowledge")
  if (!props.timeline) return <Rows {...props} memories={props.memories} />
  const periods: { period: Period; memories: MemoryRecord[] }[] = []
  for (const memory of props.memories) {
    const period = periodOf(memory.updated_at)
    const last = periods[periods.length - 1]
    if (last?.period === period) last.memories.push(memory)
    else periods.push({ period, memories: [memory] })
  }
  return (
    <div className="space-y-5">
      {periods.map(({ period, memories }) => (
        <section key={period} aria-label={t(`timeline.${period}`)}>
          <h3 className="text-n600 mb-2 text-xs font-medium">{t(`timeline.${period}`)}</h3>
          <Rows {...props} memories={memories} />
        </section>
      ))}
    </div>
  )
}

function Rows(props: MemoryListProps) {
  return (
    <ul className={card + " divide-hair divide-y overflow-hidden"}>
      {props.memories.map((memory) => (
        <MemoryRow key={memory.id} {...props} memory={memory} />
      ))}
    </ul>
  )
}

/** How the person came to have it: learned from a chat or confirmed by them; how they like to be
 *  helped; and, for a plan, its last day. */
function HowLearned({ memory }: { memory: MemoryRecord }) {
  const { t } = useTranslation("knowledge")
  const tags = [
    memory.owner === "SYSTEM_VERIFIED" ? t("how.learned") : memory.owner === "USER_CONFIRMED" ? t("how.added") : null,
    memory.fact_key?.startsWith(STYLE_PREFIX) ? t("how.style") : null,
    // A plan lasts to the end of its last day: the moment it expires is the next midnight.
    memory.expires_at ? t("how.until", { date: formatDay(new Date(Date.parse(memory.expires_at) - 1000).toISOString()) }) : null,
  ].filter((tag): tag is string => Boolean(tag))
  return (
    <>
      {tags.map((tag) => (
        <span key={tag} className="bg-hairsoft text-n700 rounded-full px-2 py-0.5">
          {tag}
        </span>
      ))}
    </>
  )
}

function MemoryRow({
  memory,
  query,
  projectId,
  busy,
  topicsOf,
  scopeName,
  onOpen,
  onEdit,
  onForget,
}: MemoryListProps & { memory: MemoryRecord }) {
  const { t } = useTranslation("knowledge")
  const hintId = useId()
  // Edit and forget are announced with the sentence they act on.
  const textId = useId()
  const back = useBackState()
  const scope = scopeName(memory)
  const topics = (topicsOf.get(memory.id) ?? []).filter((topic) => topic.page_id)
  return (
    <li className="group hover:bg-hairsoft/50 flex items-start gap-2 px-4 py-3.5 transition-colors sm:px-5">
      <div className="min-w-0 flex-1">
        {/* The sentence itself is the way in; its words stay the button's name. */}
        <button
          id={textId}
          type="button"
          aria-describedby={hintId}
          className="text-ink hover:text-a700 text-md block w-full text-start leading-relaxed break-words transition-colors"
          onClick={() => onOpen(memory)}
        >
          <Highlight text={memory.summary} query={query} />
        </button>
        <span id={hintId} hidden>
          {t("memory.openDetail")}
        </span>
        <div className="text-n600 mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          {scope && <span>{scope}</span>}
          {scope && memory.updated_at && <span aria-hidden>·</span>}
          {memory.updated_at && <time dateTime={memory.updated_at}>{formatSince(memory.updated_at)}</time>}
          <HowLearned memory={memory} />
          {topics.slice(0, MAX_TOPICS).map((topic) => (
            <Link
              key={topic.id}
              to={paths.wikiPage(topic.page_id!, projectId)}
              state={back}
              className="bg-hairsoft text-n700 hover:text-a700 inline-flex max-w-44 items-center gap-1 rounded-full px-2 py-0.5 transition-colors"
            >
              <BookOpen size={11} aria-hidden className="flex-none" />
              <span className="truncate">{topic.title}</span>
            </Link>
          ))}
          {topics.length > MAX_TOPICS && (
            <span className="text-n500">{t("memory.moreTopics", { count: topics.length - MAX_TOPICS })}</span>
          )}
        </div>
      </div>
      <div className="flex flex-none items-center gap-0.5 sm:opacity-60 sm:transition-opacity sm:group-focus-within:opacity-100 sm:group-hover:opacity-100">
        <button
          type="button"
          className={iconButton}
          aria-label={t("memory.edit")}
          aria-describedby={textId}
          title={t("memory.edit")}
          disabled={busy}
          onClick={() => onEdit(memory)}
        >
          <Pencil size={15} />
        </button>
        <button
          type="button"
          className={iconButton + " hover:text-dangerink"}
          aria-label={t("memory.forget")}
          aria-describedby={textId}
          title={t("memory.forget")}
          disabled={busy}
          onClick={() => onForget(memory)}
        >
          <Trash2 size={15} />
        </button>
      </div>
    </li>
  )
}
