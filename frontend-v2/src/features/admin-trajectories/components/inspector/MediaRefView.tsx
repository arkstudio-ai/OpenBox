import { useState } from "react"
import { useTranslation } from "react-i18next"
import { formatBytes } from "@/shared/lib/format"
import { isMediaEnvelope, type PayloadEnvelope } from "../../types/protocol"
import { mediaState, type FieldState } from "../../utils/availability"
import { AvailabilityNote } from "./AvailabilityNote"
import type { ContentEnvelope } from "./media"
import { PayloadView } from "./PayloadView"
import { NS } from "./types"

interface MediaRefViewProps {
  value: ContentEnvelope
  /** Load the protected content immediately (model input media) instead of on request (raw JSON). */
  autoLoad?: boolean
}

function payloadState(value: PayloadEnvelope): FieldState {
  const ref = value.$payload
  switch (ref.availability ?? "available") {
    case "deleted":
      return { state: "deleted", reason: ref.reason ?? null }
    case "corrupt":
      return { state: "corrupt" }
    case "unsupported":
      return { state: "unsupported" }
    case "pending":
      return { state: "pending" }
    case "not_recorded":
      return { state: "not_recorded" }
    default:
      return { state: "payload", ref }
  }
}

/**
 * Retained content referenced from inside a captured value — e.g. an image the
 * model actually received. Only the protected payload endpoint is used; a
 * wrapper without retained bytes states why instead of falling back to a URL.
 */
export function MediaRefView({ value, autoLoad = false }: MediaRefViewProps) {
  const { t } = useTranslation(NS)
  const [open, setOpen] = useState(autoLoad)
  const state = isMediaEnvelope(value) ? mediaState(value) : payloadState(value)
  const ref = isMediaEnvelope(value) ? value.$media : value.$payload
  const type =
    ref.media_type ?? (isMediaEnvelope(value) ? value.declared_media_type : null) ?? t("media.unknownType")
  const size = typeof ref.size_bytes === "number" ? formatBytes(ref.size_bytes) : t("common.dash")
  return (
    <div
      className="border-hair bg-card flex flex-col gap-1.5 rounded-lg border p-2"
      data-testid="trajectory-media-ref"
      data-state={state.state}
    >
      <p className="text-n600 text-2xs">
        {t("media.retained", { type, size })}
        {isMediaEnvelope(value) &&
          typeof value.source_kind === "string" &&
          ` · ${t("media.source", { source: value.source_kind })}`}
      </p>
      {state.state === "payload" ? (
        open ? (
          <PayloadView reference={state.ref} />
        ) : (
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="text-a700 w-fit text-xs hover:underline"
          >
            {t("media.load")}
          </button>
        )
      ) : state.state === "deleted" ? (
        <AvailabilityNote state="deleted" reason={state.reason} />
      ) : state.state === "available" ? null : (
        <AvailabilityNote state={state.state} />
      )}
    </div>
  )
}
