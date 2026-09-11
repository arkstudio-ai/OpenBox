// A first-party topic page (`/topics/:slug`): cover, title, Markdown body,
// optional call to action. Public: the API needs no bearer, so a shared link
// renders for anyone; the CTA asks for sign-in only when it leads into the app.
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router"
import ReactMarkdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"
import { useAuthStore } from "@/shared/api/auth-store"
import { paths } from "@/shared/router/paths"
import { Spinner } from "@/shared/ui/Spinner"
import { toast } from "@/shared/ui/Toast"
import { useTopic } from "../api"
import { useOpenInboxLink } from "../lib/resolveLink"

const components: Components = {
  h1: ({ children }) => <h1 className="mt-6 mb-3 text-2xl font-medium tracking-tight">{children}</h1>,
  h2: ({ children }) => <h2 className="mt-5 mb-2.5 text-xl font-medium tracking-tight">{children}</h2>,
  h3: ({ children }) => <h3 className="mt-4 mb-2 text-lg font-medium">{children}</h3>,
  p: ({ children }) => <p className="my-3 leading-7">{children}</p>,
  ul: ({ children }) => <ul className="my-3 list-disc ps-6">{children}</ul>,
  ol: ({ children }) => <ol className="my-3 list-decimal ps-6">{children}</ol>,
  li: ({ children }) => <li className="my-1">{children}</li>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer" className="text-a700 underline underline-offset-2">
      {children}
    </a>
  ),
  img: ({ src, alt }) => <img src={src} alt={alt ?? ""} className="my-4 max-w-full rounded-xl" />,
  blockquote: ({ children }) => (
    <blockquote className="border-hair text-n700 my-3 border-s-2 ps-4">{children}</blockquote>
  ),
  code: ({ children }) => (
    <code className="bg-n200 rounded px-1 py-0.5 font-mono text-[0.9em]">{children}</code>
  ),
}

export function TopicPage({ slug }: { slug: string }) {
  const { t } = useTranslation("inbox")
  const navigate = useNavigate()
  const topic = useTopic(slug)
  const open = useOpenInboxLink()
  const authenticated = useAuthStore((s) => s.isAuthenticated)

  const cta = async () => {
    const link = topic.data?.ctaLink ?? null
    if (link?.kind !== "url" && !authenticated) {
      void navigate(paths.login)
      return
    }
    if ((await open(link)) === "unavailable") toast("warning", t("unavailable"))
  }

  if (topic.isPending) {
    return (
      <div className="flex justify-center py-24">
        <Spinner className="size-5" />
      </div>
    )
  }
  if (topic.error || !topic.data) {
    return <p className="text-n600 py-24 text-center text-sm">{t("topic.notFound")}</p>
  }
  const data = topic.data
  return (
    <article className="flex flex-col">
      {data.coverUrl && (
        <img src={data.coverUrl} alt="" className="mb-5 max-h-80 w-full rounded-2xl object-cover" />
      )}
      <h1 className="text-3xl font-semibold tracking-tight">{data.title}</h1>
      <div className="text-ink mt-2 text-base">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
          {data.contentMd}
        </ReactMarkdown>
      </div>
      {data.ctaLabel && data.ctaLink && (
        <button
          type="button"
          onClick={() => void cta()}
          className="bg-ink text-bg mt-8 min-h-11 self-start rounded-full px-6 text-base"
        >
          {authenticated || data.ctaLink.kind === "url" ? data.ctaLabel : t("topic.signIn")}
        </button>
      )}
    </article>
  )
}
