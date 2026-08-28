import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { ExternalLink, RefreshCw, ShieldCheck, ShieldEllipsis, TriangleAlert } from "lucide-react"
import { cn } from "@/shared/lib/cn"
import type { MessagePart, ToolPart } from "@/shared/types/api"
import { refreshVideoIdentity, type VideoIdentity, type VideoMaterialAsset } from "../api/video-identities"
import { useSendChat } from "../hooks/useSendChat"
import { cardsFromTools } from "../lib/video-identity-card"

function identityTools(parts: MessagePart[]): ToolPart[] {
  return parts.filter((part): part is ToolPart => part.type === "tool" && part.tool === "video_identity")
}

function safeAuthorizationUrl(value?: string | null): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === "https:" ? url.toString() : null
  } catch {
    return null
  }
}

function expiryLabel(value: string | null | undefined, locale: string): string {
  if (!value) return ""
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })
}

function IdentityStatusGlyph({ status }: { status: VideoIdentity["status"] }) {
  if (status === "active") return <ShieldCheck className="size-4.5" />
  if (status === "awaiting_user") return <ShieldEllipsis className="size-4.5" />
  return <TriangleAlert className="size-4.5" />
}

function IdentityCard({
  initial,
  initialMaterial,
  sessionId,
}: {
  initial: VideoIdentity
  initialMaterial?: VideoMaterialAsset
  sessionId: string
}) {
  const { t, i18n } = useTranslation("chat")
  const sendChat = useSendChat(sessionId)
  const [identity, setIdentity] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const authorizationUrl = safeAuthorizationUrl(identity.authorization_url)
  const qrCode = identity.qr_code?.startsWith("data:image/") ? identity.qr_code : null
  const pending = identity.status === "awaiting_user"
  const active = identity.status === "active"
  const terminal = identity.status === "failed" || identity.status === "expired"

  async function refresh() {
    setBusy(true)
    setError("")
    try {
      const refreshed = await refreshVideoIdentity(identity.identity_id)
      if (refreshed.status === "active") {
        await sendChat(t("videoIdentity.continueMessage"))
      }
      setIdentity(refreshed)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("videoIdentity.refreshFailed"))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      className={cn(
        "border-hair bg-card mt-2 rounded-xl border p-4",
        active && "border-s500/35 bg-s100/35",
        terminal && "border-danger/25 bg-dangersoft/35",
      )}
      aria-label={t("videoIdentity.title")}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "bg-a100 text-a800 flex size-9 shrink-0 items-center justify-center rounded-full",
            active && "bg-s100 text-s800",
            terminal && "bg-dangersoft text-danger",
          )}
        >
          <IdentityStatusGlyph status={identity.status} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-ink text-sm font-medium">{t("videoIdentity.title")}</h3>
            <span className="bg-hairsoft text-n700 rounded-full px-2 py-0.5 text-xs">{identity.label}</span>
          </div>
          <p className="text-n700 mt-1 text-sm leading-6">{t(`videoIdentity.status.${identity.status}`)}</p>
          {pending && identity.expires_at ? (
            <p className="text-n600 mt-0.5 text-xs">
              {t("videoIdentity.expires", {
                time: expiryLabel(identity.expires_at, i18n.resolvedLanguage || "zh-CN"),
              })}
            </p>
          ) : null}
          {active ? <p className="text-s800 mt-1 text-xs">{t("videoIdentity.readyHint")}</p> : null}
          {initialMaterial?.status === "active" ? (
            <p className="text-s800 mt-1 text-xs">{t("videoIdentity.assetReady")}</p>
          ) : null}
          {(error || identity.error) && (
            <p className="text-danger mt-2 text-xs leading-5">{error || identity.error}</p>
          )}
        </div>
        {pending && qrCode ? (
          <img
            src={qrCode}
            alt={t("videoIdentity.qrAlt")}
            className="border-hair size-28 shrink-0 rounded-lg border bg-white p-1 max-sm:hidden"
          />
        ) : null}
      </div>

      {pending ? (
        <div className="mt-3 flex flex-wrap gap-2 ps-12">
          {authorizationUrl ? (
            <a
              href={authorizationUrl}
              target="_blank"
              rel="noreferrer"
              className="bg-ink text-bg hover:bg-n800 inline-flex h-9 items-center gap-1.5 rounded-full px-4 text-sm transition-colors"
            >
              {t("videoIdentity.open")}
              <ExternalLink className="size-3.5" />
            </a>
          ) : null}
          <button
            type="button"
            disabled={busy}
            onClick={refresh}
            className="border-hair text-n800 hover:border-n400 hover:text-ink inline-flex h-9 items-center gap-1.5 rounded-full border px-4 text-sm transition-colors disabled:cursor-wait disabled:opacity-60"
          >
            <RefreshCw className={cn("size-3.5", busy && "animate-spin")} />
            {busy ? t("videoIdentity.checking") : t("videoIdentity.completed")}
          </button>
        </div>
      ) : null}
    </section>
  )
}

export function VideoIdentityCards({ parts, sessionId }: { parts: MessagePart[]; sessionId: string }) {
  const tools = useMemo(() => identityTools(parts), [parts])
  const cards = useMemo(() => cardsFromTools(tools), [tools])
  if (cards.length === 0) return null
  return (
    <div className="mb-2 pe-4 sm:pe-6">
      {cards.map(({ identity, material }) => (
        <IdentityCard
          key={`${identity.identity_id}:${identity.updated_at ?? identity.status}`}
          initial={identity}
          initialMaterial={material}
          sessionId={sessionId}
        />
      ))}
    </div>
  )
}
