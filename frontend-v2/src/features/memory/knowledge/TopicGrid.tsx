import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { BookOpen, Loader } from "lucide-react"
import { formatSince } from "@/shared/lib/format"
import { paths } from "@/shared/router/paths"
import type { WikiSummary } from "../wiki-api"
import { useBackState } from "./back"
import { Highlight } from "./Highlight"
import { plainExcerpt } from "./text"

/** Topic pages as cards: a title, a few lines to recognise it by, and how
 *  well supported it is. Pages being rebuilt stay in place, quietly marked.
 *  As a `rail` (the overview's preview) a phone swipes through them sideways
 *  instead of scrolling past a tall stack. */
export function TopicGrid({
  topics,
  projectId,
  query,
  rail,
}: {
  topics: WikiSummary[]
  projectId: string
  query: string
  rail?: boolean
}) {
  return (
    <div
      className={
        rail
          ? "scr -mx-4 flex snap-x snap-mandatory gap-3 overflow-x-auto px-4 pb-2 sm:mx-0 sm:grid sm:grid-cols-2 sm:overflow-visible sm:px-0 sm:pb-0 lg:grid-cols-3"
          : "grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
      }
    >
      {topics.map((topic) => (
        <TopicCard key={topic.id} topic={topic} projectId={projectId} query={query} rail={rail} />
      ))}
    </div>
  )
}

function TopicCard({
  topic,
  projectId,
  query,
  rail,
}: {
  topic: WikiSummary
  projectId: string
  query: string
  rail?: boolean
}) {
  const { t } = useTranslation("knowledge")
  const back = useBackState()
  const ready = topic.body_available
  return (
    <Link
      to={paths.wikiPage(topic.id, projectId)}
      state={back}
      className={
        (rail ? "w-[78%] flex-none snap-start sm:w-auto " : "") +
        "group flex min-h-40 min-w-0 flex-col rounded-2xl border p-4 transition-colors sm:p-5 " +
        (ready
          ? "border-hair bg-card hover:border-n400"
          : "border-hair bg-hairsoft/40 border-dashed hover:border-n400")
      }
    >
      <span
        className={
          "flex size-8 items-center justify-center rounded-xl " +
          (ready ? "bg-a100 text-a700" : "bg-hairsoft text-n500")
        }
      >
        <BookOpen size={16} aria-hidden />
      </span>
      <h3
        className={
          "mt-3 line-clamp-2 text-lg leading-snug font-medium break-words transition-colors " +
          (ready ? "text-ink group-hover:text-a700" : "text-n700")
        }
      >
        <Highlight text={topic.title} query={query} />
      </h3>
      {ready ? (
        <p className="text-n600 mt-1.5 line-clamp-3 text-sm leading-relaxed break-words">
          {plainExcerpt(topic.excerpt, topic.title)}
        </p>
      ) : (
        <p className="text-n600 mt-1.5 text-sm leading-relaxed">{t("topic.updatingHint")}</p>
      )}
      <p className="text-n500 mt-auto flex items-center gap-1.5 pt-4 text-xs">
        {ready ? (
          <>
            {t("topic.sources", { count: topic.source_count })}
            {topic.updated_at && (
              <>
                <span aria-hidden>·</span>
                <time dateTime={topic.updated_at}>{formatSince(topic.updated_at)}</time>
              </>
            )}
          </>
        ) : (
          <>
            <Loader size={12} aria-hidden />
            {t("topic.updating")}
          </>
        )}
      </p>
    </Link>
  )
}
