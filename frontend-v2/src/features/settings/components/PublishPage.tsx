import { useTranslation } from "react-i18next"
import { cn } from "@/shared/lib/cn"
import {
  usePublishRoute,
  useUpdatePublishRoute,
  type PublishRoute,
} from "@/features/settings/api/publish"

const ROUTES: PublishRoute[] = ["desktop", "api"]

/** One selectable route card: title, one-line explanation, default tag. */
function RouteCard({
  route,
  active,
  isDefault,
  onPick,
}: {
  route: PublishRoute
  active: boolean
  isDefault: boolean
  onPick: () => void
}) {
  const { t } = useTranslation("settings")
  return (
    <button
      type="button"
      onClick={onPick}
      aria-pressed={active}
      className={cn(
        "flex flex-col gap-1 rounded-lg border bg-card px-4 py-3.5 text-start",
        active ? "border-ink" : "border-hair",
      )}
    >
      <span className="flex items-center gap-2 text-base">
        {t(`publish.route.${route}`)}
        {isDefault && (
          <span className="rounded-full bg-n200 px-2 py-0.5 text-2xs font-medium text-n700">
            {t("publish.defaultTag")}
          </span>
        )}
      </span>
      <span className="text-pretty text-xs text-n600">{t(`publish.desc.${route}`)}</span>
    </button>
  )
}

export function PublishPage() {
  const { t } = useTranslation("settings")
  const status = usePublishRoute()
  const update = useUpdatePublishRoute()

  const deploymentDefault = status.data?.deploymentDefault ?? "desktop"
  const preference = status.data?.preference ?? null
  const effective = status.data?.effective ?? deploymentDefault

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2.5">
        {ROUTES.map((route) => (
          <RouteCard
            key={route}
            route={route}
            active={effective === route}
            isDefault={deploymentDefault === route}
            onPick={() => update.mutate(route)}
          />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-n600">
        <span>
          {preference
            ? t("publish.chosen", { route: t(`publish.route.${preference}`) })
            : t("publish.followingDefault", { route: t(`publish.route.${deploymentDefault}`) })}
        </span>
        {preference && (
          <button
            type="button"
            disabled={update.isPending}
            onClick={() => update.mutate(null)}
            className="text-n700 underline-offset-2 hover:underline disabled:opacity-50"
          >
            {t("publish.resetToDefault")}
          </button>
        )}
      </div>

      <p className="text-pretty text-xs text-n600">{t("publish.note")}</p>
    </div>
  )
}
