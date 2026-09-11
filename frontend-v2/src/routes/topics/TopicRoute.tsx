// Public topic page outside the workspace shell: a brand header and the
// article. Signed-in visitors get a way back into the app.
import { useTranslation } from "react-i18next"
import { Link, useParams } from "react-router"
import { TopicPage } from "@/features/inbox"
import { useAuthStore } from "@/shared/api/auth-store"
import { BrandMark } from "@/shared/ui/BrandMark"
import { paths } from "@/shared/router/paths"

export default function TopicRoute() {
  const { t } = useTranslation("inbox")
  const { slug = "" } = useParams()
  const authenticated = useAuthStore((s) => s.isAuthenticated)
  return (
    <div className="scr bg-bg text-ink flex min-h-screen flex-col overflow-x-hidden">
      <header className="border-hair flex items-center justify-between border-b px-6 py-3.5">
        <Link to={paths.landing} aria-label={t("topic.enterApp")}>
          <BrandMark />
        </Link>
        <Link
          to={authenticated ? paths.app : paths.login}
          className="border-hair hover:bg-hairsoft min-h-9 rounded-full border px-4 text-sm leading-9"
        >
          {t(authenticated ? "topic.enterApp" : "topic.signIn")}
        </Link>
      </header>
      <main className="mx-auto w-full max-w-[720px] flex-1 px-6 py-8">
        <TopicPage slug={slug} />
      </main>
    </div>
  )
}
