import { useTranslation } from "react-i18next"
import { formatBytes } from "@/shared/lib/format"
import type { ReviewFile } from "@/features/admin-skills/types"

interface Props {
  files: readonly ReviewFile[]
  /** What the archive declares. The rows stop at the server's cap; the count must not. */
  total?: number
  /** The server stops listing at 500 entries; say so rather than imply completeness. */
  truncated?: boolean
}

/** The archive's directory, read without unpacking it (plan §4.9). */
export function ReviewFiles({ files, total, truncated }: Props) {
  const { t } = useTranslation("admin-skills")
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-n600 text-xs font-medium">{t("review.files", { count: total ?? files.length })}</h3>
      <div className="border-hair max-h-64 overflow-auto rounded-lg border">
        <table className="w-full text-start text-xs">
          <thead className="text-n500">
            <tr>
              <th scope="col" className="px-3 py-1.5">
                {t("review.fileColumn.path")}
              </th>
              <th scope="col" className="px-3 py-1.5 text-end">
                {t("review.fileColumn.size")}
              </th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file.path} className="border-hair border-t">
                <td className="px-3 py-1.5 font-mono break-all">{file.path}</td>
                <td className="text-n600 px-3 py-1.5 text-end whitespace-nowrap">{formatBytes(file.size)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {truncated && <p className="text-n600 text-2xs">{t("review.filesTruncated")}</p>}
    </section>
  )
}
