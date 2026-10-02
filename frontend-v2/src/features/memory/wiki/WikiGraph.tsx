import { Link } from "react-router"
import { useTranslation } from "react-i18next"
import { paths } from "@/shared/router/paths"
import type { WikiSummary } from "../wiki-api"
import { pageConnections } from "./content"

export function WikiGraph({ pages, projectId }: { pages: WikiSummary[]; projectId: string }) {
  const { t } = useTranslation("wiki")
  const nodes = pages.slice(0, 40)
  const edges = pageConnections(nodes)
  const points = new Map(
    nodes.map((page, i) => [
      page.id,
      {
        x: 340 + Math.cos((i * 2 * Math.PI) / nodes.length - Math.PI / 2) * 220,
        y: 235 + Math.sin((i * 2 * Math.PI) / nodes.length - Math.PI / 2) * 165,
      },
    ]),
  )
  return (
    <section className="border-hair bg-card min-w-0 rounded-2xl border p-5 sm:p-7" aria-label={t("graph")}>
      <h2 className="text-xl font-semibold">{t("graph")}</h2>
      <p className="text-n600 mt-2 max-w-xl text-sm leading-6">{t("graphHint")}</p>
      {!nodes.length ? (
        <p className="text-n500 py-12 text-center">{t("emptyGraph")}</p>
      ) : (
        <>
          <svg
            viewBox="0 0 680 470"
            role="group"
            aria-label={t("graphDiagram")}
            className="my-5 max-h-110 w-full"
          >
            {edges.map((edge) => (
              <line
                key={edge.from.id + edge.to.id}
                x1={points.get(edge.from.id)!.x}
                y1={points.get(edge.from.id)!.y}
                x2={points.get(edge.to.id)!.x}
                y2={points.get(edge.to.id)!.y}
                className="stroke-n300"
                strokeWidth="1.5"
              />
            ))}
            {nodes.map((page) => {
              const point = points.get(page.id)!
              return (
                <Link
                  key={page.id}
                  to={paths.wikiPage(page.id, projectId)}
                  className="group"
                  aria-label={t("graphNode", {
                    title: page.title,
                    status: t(page.body_available ? "published" : "stale"),
                  })}
                >
                  <title>{page.title}</title>
                  <circle
                    cx={point.x}
                    cy={point.y}
                    r="12"
                    className={page.body_available ? "fill-accent stroke-card" : "fill-n300 stroke-card"}
                    strokeWidth="3"
                  />
                  <text
                    x={point.x}
                    y={point.y + 30}
                    textAnchor="middle"
                    className={
                      "fill-ink text-xs " +
                      (nodes.length > 8 ? "opacity-0 group-hover:opacity-100 group-focus:opacity-100" : "")
                    }
                  >
                    {page.title.length > 14 ? page.title.slice(0, 14) + "…" : page.title}
                  </text>
                </Link>
              )
            })}
          </svg>
          <h3 className="mb-3 text-sm font-medium">{t("connections")}</h3>
          {edges.length ? (
            <ul className="divide-hair divide-y">
              {edges.map((edge) => (
                <li
                  key={edge.from.id + edge.to.id}
                  className="flex flex-wrap items-center gap-2 py-3 text-sm"
                >
                  <Link className="text-a700 hover:underline" to={paths.wikiPage(edge.from.id, projectId)}>
                    {edge.from.title}
                  </Link>
                  <span className="text-n500">↔</span>
                  <Link className="text-a700 hover:underline" to={paths.wikiPage(edge.to.id, projectId)}>
                    {edge.to.title}
                  </Link>
                  <span className="text-n500 ms-auto text-xs">
                    {t("sharedSources", { count: edge.sources })}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-n500 text-sm">{t("noConnections")}</p>
          )}
          <p className="text-n500 mt-5 text-xs">{t("graphLimit", { count: nodes.length })}</p>
        </>
      )}
    </section>
  )
}
