// Every admin column is the same shape — a wide, dense, read-mostly table with
// four states — so the shell lives here instead of being retyped per page
// (ENGINEERING_SPEC §18: four states are a hard requirement, and hand-rolled
// tables kept forgetting one).
import type { ReactNode } from "react"
import { cn } from "@/shared/lib/cn"
import { Spinner } from "@/shared/ui/Spinner"

export interface DataTableColumn<T> {
  /** Stable id; also the field read when the column has no `render`. */
  key: string
  header: ReactNode
  /** Applied to this column's header cell and every body cell (e.g. "font-mono"). */
  className?: string
  render?: (row: T) => ReactNode
}

export interface DataTableProps<T> {
  columns: ReadonlyArray<DataTableColumn<T>>
  rows: readonly T[]
  rowKey: (row: T) => string
  /**
   * Makes rows clickable and keyboard-reachable. Controls inside a cell must
   * `stopPropagation()` on their own click, or pressing them opens the row too.
   */
  onRowClick?: (row: T) => void
  isLoading?: boolean
  error?: unknown
  emptyText: string
  errorText: string
  /** Screen-reader text for the loading state; the spinner alone says nothing. */
  loadingLabel: string
  /** Tailwind min-width class for the inner table, e.g. "min-w-[60rem]". */
  minWidth?: string
}

/** Last-resort cell content: stringify the field named by the column key. */
function cellValue<T>(row: T, key: string): ReactNode {
  const value = (row as unknown as Record<string, unknown>)[key]
  return value === null || value === undefined ? null : String(value)
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  isLoading,
  error,
  emptyText,
  errorText,
  loadingLabel,
  minWidth,
}: DataTableProps<T>) {
  // Loading wins over error so a retry shows progress rather than the stale
  // failure it is already busy replacing.
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Spinner className="size-5" />
        <span className="sr-only">{loadingLabel}</span>
      </div>
    )
  }
  if (error) {
    return (
      <p role="alert" className="py-10 text-center text-sm text-danger">
        {errorText}
      </p>
    )
  }
  if (rows.length === 0) {
    return <p className="py-10 text-center text-sm text-n600">{emptyText}</p>
  }

  return (
    <div className="overflow-x-auto">
      <table className={cn("w-full text-start text-xs", minWidth)}>
        <thead className="text-n500">
          <tr>
            {columns.map((column) => (
              <th key={column.key} scope="col" className={cn("pb-2 pe-3", column.className)}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              className={cn(
                "border-t border-hair align-top",
                onRowClick &&
                  "cursor-pointer hover:bg-hairsoft focus-visible:outline-2 focus-visible:outline-a700",
              )}
              tabIndex={onRowClick ? 0 : undefined}
              onClick={onRowClick && (() => onRowClick(row))}
              onKeyDown={
                onRowClick &&
                ((event) => {
                  // Only the row itself: Enter on a button inside a cell is that
                  // button's business, and it bubbles up here.
                  if (event.target !== event.currentTarget) return
                  if (event.key !== "Enter" && event.key !== " ") return
                  event.preventDefault()
                  onRowClick(row)
                })
              }
            >
              {columns.map((column) => (
                <td key={column.key} className={cn("py-2.5 pe-3", column.className)}>
                  {column.render ? column.render(row) : cellValue(row, column.key)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
