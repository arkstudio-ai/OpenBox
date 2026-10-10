import { useTranslation } from "react-i18next"
import { LEGAL_DOCUMENTS, legalPath } from "./links"

export function LegalLinks({ compact = false }: { compact?: boolean }) {
  const { t, i18n } = useTranslation("legal")
  const documents = compact ? (["terms", "privacy", "ai"] as const) : LEGAL_DOCUMENTS
  return (
    <nav aria-label={t("title")} className="text-n700 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
      {documents.map((id) => (
        <a
          key={id}
          href={legalPath(id, i18n.language)}
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-ink min-h-8 content-center underline-offset-4 hover:underline"
        >
          {t(`documents.${id}.title`)}
        </a>
      ))}
    </nav>
  )
}

export function LegalFooter() {
  const { t } = useTranslation("legal")
  return (
    <div className="border-hair text-n700 space-y-3 border-t pt-4 text-xs">
      <LegalLinks />
      <div className="flex flex-wrap gap-x-4 gap-y-2">
        <span>{t("operator")}</span>
        <a
          href={t("icpUrl")}
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-ink underline-offset-4 hover:underline"
        >
          {t("siteIcp")}
        </a>
      </div>
    </div>
  )
}

export function LegalConsent({
  accepted,
  onChange,
}: {
  accepted: boolean
  onChange: (value: boolean) => void
}) {
  const { t } = useTranslation("legal")
  return (
    <div className="my-4 space-y-2">
      <label className="text-n700 flex items-start gap-2 text-xs leading-relaxed">
        <input
          type="checkbox"
          checked={accepted}
          onChange={(e) => onChange(e.target.checked)}
          className="accent-ink mt-0.5 size-4 shrink-0"
        />
        <span>{t("consentLabel")}</span>
      </label>
      <LegalLinks compact />
    </div>
  )
}
