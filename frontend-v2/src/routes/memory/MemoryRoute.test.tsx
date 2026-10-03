import { cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router"
import { afterEach, expect, it } from "vitest"
import MemoryRoute from "./MemoryRoute"

function Where() {
  const location = useLocation()
  return <p>{location.pathname + location.search}</p>
}

afterEach(cleanup)

it.each([
  ["/app/memory", "/app/wiki?view=memories"],
  ["/app/memory?project=p1", "/app/wiki?project=p1&view=memories"],
])("sends the old memory page %s to the knowledge page's memories", (entry, target) => {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/app/memory" element={<MemoryRoute />} />
        <Route path="/app/wiki" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
  expect(screen.getByText(target)).toBeTruthy()
})
