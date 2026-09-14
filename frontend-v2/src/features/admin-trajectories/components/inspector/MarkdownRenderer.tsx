import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import ReactMarkdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"

interface MarkdownRendererProps {
  text: string
}

const NAVIGABLE = /^(https?:|mailto:)/i

// Recorded content belongs to someone else's session. Raw HTML is dropped, an
// image reference is shown as inert text (loading it would fetch a remote or
// current-user resource the recording never retained), and only absolute
// http(s)/mailto links stay clickable — opened without opener or referrer.
export default function MarkdownRenderer({ text }: MarkdownRendererProps) {
  const { t } = useTranslation("admin-trajectories")
  const components = useMemo<Components>(
    () => ({
      img: ({ alt, src }) => (
        <span className="text-n500 italic" data-blocked-image="">
          {t("markdown.imageBlocked", { alt: alt ?? "", source: typeof src === "string" ? src : "" })}
        </span>
      ),
      a: ({ href, children }) =>
        href && NAVIGABLE.test(href) ? (
          <a href={href} target="_blank" rel="noopener noreferrer nofollow" referrerPolicy="no-referrer">
            {children}
          </a>
        ) : (
          <span className="underline decoration-dotted">{children}</span>
        ),
    }),
    [t],
  )
  return (
    <div className="text-ink [&_a]:text-a700 [&_pre]:bg-surface [&_td]:border-hair [&_th]:border-hair text-sm leading-6 break-words [&_a]:underline [&_code]:font-mono [&_code]:text-xs [&_h1]:text-base [&_h1]:font-medium [&_h2]:text-sm [&_h2]:font-medium [&_li]:ms-4 [&_ol]:list-decimal [&_p]:my-1.5 [&_pre]:overflow-auto [&_pre]:rounded-lg [&_pre]:p-3 [&_table]:text-xs [&_td]:border [&_td]:px-2 [&_th]:border [&_th]:px-2 [&_ul]:list-disc">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={components}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
