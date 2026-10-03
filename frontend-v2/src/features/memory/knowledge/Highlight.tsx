import { splitMatches } from "./text"

/** Text with the current search term marked, so a hit is visible at a glance. */
export function Highlight({ text, query }: { text: string; query: string }) {
  return (
    <>
      {splitMatches(text, query).map((part, index) =>
        part.hit ? (
          <mark key={index} className="bg-a300 text-ink rounded-sm px-0.5">
            {part.text}
          </mark>
        ) : (
          part.text
        ),
      )}
    </>
  )
}
