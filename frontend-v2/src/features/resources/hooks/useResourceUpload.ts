// Upload queue for the centre. Each file is its own transfer to OSS, so one
// failure never takes the batch down; finished files invalidate the listing.
import { useCallback, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "@/shared/ui/Toast"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { uploadResource } from "@/features/resources/api/upload"
import { useRefreshResources } from "@/features/resources/api/resources"

export interface UploadTask {
  key: string
  name: string
  progress: number
  failed: boolean
}

let seq = 0

export function useResourceUpload(projectId: string | null) {
  const { t } = useTranslation("resources")
  const [tasks, setTasks] = useState<UploadTask[]>([])
  const refresh = useRefreshResources()
  const errorMessage = useApiErrorMessage()

  const upload = useCallback(
    (files: File[]) => {
      for (const file of files) {
        const key = `up-${++seq}`
        setTasks((list) => [...list, { key, name: file.name, progress: 0, failed: false }])
        const patch = (update: Partial<UploadTask>) =>
          setTasks((list) => list.map((task) => (task.key === key ? { ...task, ...update } : task)))

        void (async () => {
          try {
            await uploadResource(file, projectId, (fraction) => patch({ progress: fraction }))
            setTasks((list) => list.filter((task) => task.key !== key))
            refresh()
          } catch (err) {
            patch({ failed: true })
            toast("error", `${t("upload.failed", { name: file.name })} — ${errorMessage(err)}`)
            // Leave the failed chip up briefly so the row is not just a toast.
            window.setTimeout(() => setTasks((list) => list.filter((task) => task.key !== key)), 4000)
          }
        })()
      }
    },
    [projectId, refresh, t, errorMessage],
  )

  return { tasks, upload, uploading: tasks.some((task) => !task.failed) }
}
