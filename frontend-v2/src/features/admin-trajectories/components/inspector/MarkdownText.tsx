import { lazy, Suspense } from "react"
import { TextBlock } from "./TextBlock"

// The Markdown stack stays out of the console's first chunk (§16.2).
const MarkdownRenderer = lazy(() => import("./MarkdownRenderer"))

interface MarkdownTextProps {
  text: string
}

export function MarkdownText({ text }: MarkdownTextProps) {
  return (
    <Suspense fallback={<TextBlock text={text} mono={false} />}>
      <MarkdownRenderer text={text} />
    </Suspense>
  )
}
