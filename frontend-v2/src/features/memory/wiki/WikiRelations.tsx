import { useState } from "react"
import { useInfiniteQuery } from "@tanstack/react-query"
import { useTranslation } from "react-i18next"
import { Link } from "react-router"
import { paths } from "@/shared/router/paths"
import { MemoryStatus, memoryButton, memoryPrimary } from "@/shared/ui/MemoryDiagnostics"
import { useMemoryScope } from "../api"
import type { WikiSummary } from "../wiki-api"
import { WikiGraph } from "./WikiGraph"
import { platformApi, type Relation } from "./platform-api"
import { panel, useWikiAction, WikiError } from "./platform-ui"

export function WikiConnections({ pages, projectId }: { pages: WikiSummary[]; projectId: string }) {
  const { t } = useTranslation("wiki")
  const [semantic, setSemantic] = useState(true)
  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <button
          aria-pressed={semantic}
          className={semantic ? memoryPrimary : memoryButton}
          onClick={() => setSemantic(true)}
        >
          {t("semanticRelations")}
        </button>
        <button
          aria-pressed={!semantic}
          className={!semantic ? memoryPrimary : memoryButton}
          onClick={() => setSemantic(false)}
        >
          {t("connections")}
        </button>
      </div>
      {semantic ? (
        <WikiSemanticRelations projectId={projectId} />
      ) : (
        <WikiGraph pages={pages} projectId={projectId} />
      )}
    </div>
  )
}

export function WikiSemanticRelations({ projectId, pageId }: { projectId: string; pageId?: string }) {
  const { t } = useTranslation("wiki")
  const { key } = useMemoryScope()
  const query = useInfiniteQuery({
    queryKey: [...key, "wiki-semantic-relations", projectId],
    queryFn: ({ pageParam }) => platformApi.relations(projectId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset ?? undefined,
    refetchInterval: 10000,
  })
  const relations = (
    query.error ? [] : (query.data?.pages.flatMap((page) => page.relations ?? []) ?? [])
  ).filter((relation) => !pageId || relation.from_page_id === pageId || relation.to_page_id === pageId)
  return (
    <section className={panel} aria-label={t("semanticRelations")}>
      <h2 className="text-xl font-semibold">{t("semanticRelations")}</h2>
      <p className="text-n500 text-sm leading-6">{t("semanticRelationsHint")}</p>
      <WikiError error={query.error} />
      {!pageId && !!relations.length && <SemanticDiagram relations={relations} />}
      {!relations.length && (
        <p className="text-n500 text-sm">{t(query.isPending ? "loading" : "noSemanticRelations")}</p>
      )}
      {relations.map((relation) => (
        <RelationRow key={relation.id} relation={relation} />
      ))}
      {query.hasNextPage && (
        <button
          className={memoryButton}
          disabled={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          {t("loadMore")}
        </button>
      )}
    </section>
  )
}

function RelationRow({ relation }: { relation: Relation }) {
  const { t } = useTranslation("wiki")
  const decision = useWikiAction((action: "approve" | "reject") =>
    platformApi.relationDecision(relation, action),
  )
  return (
    <article className="border-hair space-y-3 border-t pt-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Endpoint
          title={relation.from_title}
          pageId={relation.from_page_id}
          projectId={relation.project_id}
        />
        <span className="text-n500">
          {t("relationTypes." + relation.type, { defaultValue: relation.type })} →
        </span>
        <Endpoint title={relation.to_title} pageId={relation.to_page_id} projectId={relation.project_id} />
        <MemoryStatus status={relation.status} />
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer">{t("evidence")}</summary>
        {relation.evidence.map((item, index) => (
          <blockquote key={index} className="text-n600 my-3 leading-6">
            {item.quote}
          </blockquote>
        ))}
      </details>
      <WikiError error={decision.error} />
      {relation.status === "proposed" && (
        <div className="flex gap-2">
          <button
            className={memoryButton}
            disabled={!relation.resolved || decision.isPending}
            onClick={() => decision.mutate("approve")}
          >
            {t("approveRelation")}
          </button>
          <button
            className={memoryButton}
            disabled={decision.isPending}
            onClick={() => decision.mutate("reject")}
          >
            {t("reject")}
          </button>
        </div>
      )}
      {!relation.resolved && <p className="text-n500 text-xs">{t("unresolvedRelation")}</p>}
    </article>
  )
}

function Endpoint({
  title,
  pageId,
  projectId,
}: {
  title: string
  pageId: string | null
  projectId: string | null
}) {
  return pageId ? (
    <Link className="text-a700 underline" to={paths.wikiPage(pageId, projectId ?? "")}>
      {title}
    </Link>
  ) : (
    <span>{title}</span>
  )
}

function SemanticDiagram({ relations }: { relations: Relation[] }) {
  const { t } = useTranslation("wiki")
  const nodes = Array.from(
    new Map(
      relations.flatMap((relation) => [
        [
          relation.from_id,
          {
            id: relation.from_id,
            title: relation.from_title,
            page: relation.from_page_id,
            project: relation.project_id,
          },
        ] as const,
        [
          relation.to_id,
          {
            id: relation.to_id,
            title: relation.to_title,
            page: relation.to_page_id,
            project: relation.project_id,
          },
        ] as const,
      ]),
    ).values(),
  ).slice(0, 40)
  const points = new Map(
    nodes.map((node, index) => [
      node.id,
      {
        x: 340 + 240 * Math.cos((index / nodes.length) * 2 * Math.PI),
        y: 220 + 150 * Math.sin((index / nodes.length) * 2 * Math.PI),
      },
    ]),
  )
  return (
    <>
      <svg viewBox="0 0 680 440" role="group" aria-label={t("semanticDiagram")} className="max-h-96 w-full">
        {relations
          .filter((relation) => points.has(relation.from_id) && points.has(relation.to_id))
          .map((relation) => (
            <line
              key={relation.id}
              x1={points.get(relation.from_id)!.x}
              y1={points.get(relation.from_id)!.y}
              x2={points.get(relation.to_id)!.x}
              y2={points.get(relation.to_id)!.y}
              strokeDasharray={relation.status === "proposed" ? "5 4" : undefined}
              className={relation.status === "active" ? "stroke-accent" : "stroke-n300"}
            >
              <title>{relation.from_title + " → " + relation.to_title}</title>
            </line>
          ))}
        {nodes.map((node) => (
          <g key={node.id}>
            <circle cx={points.get(node.id)!.x} cy={points.get(node.id)!.y} r={9} className="fill-accent" />
            <text
              x={points.get(node.id)!.x}
              y={points.get(node.id)!.y + 24}
              textAnchor="middle"
              className="fill-ink text-xs"
            >
              {node.page ? (
                <Link to={paths.wikiPage(node.page, node.project ?? "")}>{node.title?.slice(0, 14)}</Link>
              ) : (
                node.title?.slice(0, 14)
              )}
            </text>
          </g>
        ))}
      </svg>
      <p className="text-n500 text-xs">{t("semanticLegend", { count: nodes.length })}</p>
    </>
  )
}
