// Middle column: title bar, the filter strip and the rows — DEEIX's file
// pane, rebuilt on OpenBox tokens. Its width is draggable (ColumnResizer),
// because how much of a filename fits is the whole question here.
import { useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Plus, Search } from "lucide-react"
import { Spinner } from "@/shared/ui/Spinner"
import { cn } from "@/shared/lib/cn"
import { ColumnResizer } from "./ColumnResizer"
import { ListToolbar } from "./ListToolbar"
import { ResourceRow } from "./ResourceRow"
import type { ResourceFilterActions, ResourceFilters } from "../hooks/useResourceFilters"
import type { UploadTask } from "../hooks/useResourceUpload"
import { useResourcesUi } from "../stores/ui"
import type { Resource } from "../types"

export interface SelectionApi {
  ids: string[]
  toggle: (id: string, checked: boolean) => void
  clear: () => void
  selectAll: () => void
}

interface Props {
  list: {
    items: Resource[]
    loading: boolean
    total: number
    hasMore: boolean
    loadingMore: boolean
  }
  filters: ResourceFilters
  actions: ResourceFilterActions
  selection: SelectionApi
  upload: { tasks: UploadTask[]; upload: (files: File[]) => void }
  mutations: {
    rename: (id: string, name: string) => void
    remove: (resource: Resource) => void
    removeMany: (ids: string[]) => void
    loadMore: () => void
  }
}

export function ResourceList({ list, filters, actions, selection, upload, mutations }: Props) {
  const { t } = useTranslation("resources")
  const fileRef = useRef<HTMLInputElement>(null)
  const [searching, setSearching] = useState(false)
  const checked = new Set(selection.ids)
  const width = useResourcesUi((s) => s.listWidth)
  const setWidth = useResourcesUi((s) => s.setListWidth)

  return (
    <section
      className="border-hair relative flex flex-none flex-col border-e"
      style={{ width }}
    >
      <input
        ref={fileRef}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          upload.upload([...(e.target.files ?? [])])
          e.target.value = ""
        }}
      />

      <div className="flex h-11 items-center gap-1 px-3">
        <span className="min-w-0 flex-1 truncate text-lg font-medium tracking-tight">{t("list.title")}</span>
        <button
          type="button"
          onClick={() => setSearching((v) => !v)}
          aria-label={t("actions.search")}
          title={t("actions.search")}
          className={cn(
            "text-n700 hover:bg-hairsoft flex size-7 items-center justify-center rounded-full",
            searching && "bg-n200 text-ink",
          )}
        >
          <Search className="size-4" strokeWidth={2.1} />
        </button>
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          aria-label={t("actions.upload")}
          title={t("actions.upload")}
          className="text-n700 hover:bg-hairsoft flex size-7 items-center justify-center rounded-full"
        >
          <Plus className="size-4.5" strokeWidth={2.4} />
        </button>
      </div>

      {searching && (
        <div className="px-3 pb-2">
          <input
            value={filters.q}
            onChange={(e) => actions.setQuery(e.target.value)}
            placeholder={t("list.searchPlaceholder")}
            className="border-hair bg-bg text-ink placeholder:text-n600 focus:border-n400 h-8 w-full rounded-full border px-3 text-xs outline-none"
            autoFocus
          />
        </div>
      )}

      <ListToolbar
        kind={filters.kind}
        sort={filters.sort}
        selectedCount={selection.ids.length}
        selectAllDisabled={list.items.length === 0}
        onKind={actions.setKind}
        onSort={actions.setSort}
        onSelectAll={selection.selectAll}
        onClearSelection={selection.clear}
        onDeleteSelected={() => mutations.removeMany(selection.ids)}
      />

      <div className="scr flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-1.5 pb-3">
        {upload.tasks.map((task) => (
          <div key={task.key} className="text-n600 flex h-9 items-center gap-2 rounded-lg px-2 text-xs">
            <Spinner className="size-3.5 flex-none" />
            <span className={cn("min-w-0 flex-1 truncate", task.failed && "text-dangerink")}>
              {task.name}
            </span>
            <span className="text-2xs flex-none">
              {task.failed ? t("upload.failedShort") : `${Math.round(task.progress * 100)}%`}
            </span>
          </div>
        ))}

        {list.loading ? (
          <div className="flex flex-1 items-center justify-center py-8">
            <Spinner className="size-4" />
          </div>
        ) : list.items.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-1 px-4 py-10 text-center">
            <span className="text-md text-ink">{t("list.empty")}</span>
            <span className="text-2xs text-n600">{t("list.emptyHint")}</span>
          </div>
        ) : (
          <>
            {list.items.map((resource) => (
              <ResourceRow
                key={resource.id}
                resource={resource}
                active={resource.id === filters.selected}
                checked={checked.has(resource.id)}
                onOpen={actions.select}
                onToggle={selection.toggle}
                onRename={mutations.rename}
                onDelete={mutations.remove}
              />
            ))}
            {list.hasMore ? (
              <button
                type="button"
                onClick={mutations.loadMore}
                disabled={list.loadingMore}
                className="text-2xs text-n600 hover:text-ink py-3 text-center disabled:opacity-60"
              >
                {list.loadingMore
                  ? t("list.loadingMore")
                  : t("list.loadMore", { shown: list.items.length, total: list.total })}
              </button>
            ) : (
              <span className="text-2xs text-n600 py-3 text-center">
                {t("list.allLoaded", { count: list.total })}
              </span>
            )}
          </>
        )}
      </div>

      <ColumnResizer width={width} onWidth={setWidth} label={t("actions.resize")} />
    </section>
  )
}
