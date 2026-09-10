import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Dialog, DialogTitle } from "@/shared/ui/Dialog"
import { ArchiveUploadQueue } from "@/shared/ui/ArchiveUploadQueue"
import { useUploadEntries } from "../api/management"

export function StoreUpload({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation("admin-skills")
  const upload = useUploadEntries()
  const [busy, setBusy] = useState(false)
  return (
    <Dialog
      open
      wide
      label={t("manage.upload")}
      onClose={() => {
        if (!busy) onClose()
      }}
    >
      <DialogTitle>{t("manage.upload")}</DialogTitle>
      <p className="text-n600 text-sm">{t("manage.uploadHint")}</p>
      <ArchiveUploadQueue upload={(files) => upload.mutateAsync(files)} onBusyChange={setBusy} />
      <button
        disabled={busy}
        className="self-end rounded-full px-4 py-2 text-sm disabled:opacity-40"
        onClick={onClose}
      >
        {t("manage.close")}
      </button>
    </Dialog>
  )
}
