import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { DataTable, type DataTableColumn, type DataTableProps } from "./DataTable"

interface Row {
  id: string
  name: string
  version: string
}

const rows: Row[] = [
  { id: "a", name: "alpha", version: "1.0.0" },
  { id: "b", name: "beta", version: "2.1.0" },
]

const columns: DataTableColumn<Row>[] = [
  { key: "name", header: "名称", render: (row) => row.name },
  { key: "version", header: "版本", className: "font-mono" },
]

function mount(props: Partial<DataTableProps<Row>> = {}) {
  return render(
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(row) => row.id}
      emptyText="商店里还没有条目。"
      errorText="加载失败"
      loadingLabel="加载中"
      {...props}
    />,
  )
}

afterEach(cleanup)

describe("DataTable", () => {
  it("renders rows, falling back to the field named by the column key", () => {
    const { container } = mount()
    expect(screen.getByText("alpha")).toBeDefined()
    expect(screen.getByText("2.1.0")).toBeDefined()
    // Header plus one row each.
    expect(screen.getAllByRole("row").length).toBe(3)
    expect(screen.getByText("1.0.0").className).toContain("font-mono")
    // Wide tables scroll inside their own box, never sideways over the page.
    expect(container.querySelector(".overflow-x-auto table")).not.toBeNull()
  })

  it("shows a labelled spinner while loading, and no rows", () => {
    mount({ isLoading: true })
    expect(screen.getByText("加载中")).toBeDefined()
    expect(screen.queryByRole("table")).toBeNull()
    expect(screen.queryByText("alpha")).toBeNull()
  })

  it("reports an error instead of claiming the list is empty", () => {
    mount({ rows: [], error: new Error("offline") })
    expect(screen.getByRole("alert").textContent).toBe("加载失败")
    expect(screen.queryByText("商店里还没有条目。")).toBeNull()
  })

  it("explains an empty list", () => {
    mount({ rows: [] })
    expect(screen.getByText("商店里还没有条目。")).toBeDefined()
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("opens a row by click and by keyboard, and is reachable by tab", () => {
    const onRowClick = vi.fn()
    mount({ onRowClick })
    const [, first, second] = screen.getAllByRole("row")
    expect(first.getAttribute("tabindex")).toBe("0")
    expect(first.className).toContain("focus-visible:outline-a700")

    fireEvent.click(first)
    fireEvent.keyDown(second, { key: "Enter" })
    fireEvent.keyDown(second, { key: " " })
    fireEvent.keyDown(second, { key: "a" })
    expect(onRowClick.mock.calls.map(([row]) => row.id)).toEqual(["a", "b", "b"])
  })

  it("leaves rows inert when there is nothing to open", () => {
    mount()
    const [, first] = screen.getAllByRole("row")
    expect(first.getAttribute("tabindex")).toBeNull()
    expect(first.className).not.toContain("cursor-pointer")
  })
})
