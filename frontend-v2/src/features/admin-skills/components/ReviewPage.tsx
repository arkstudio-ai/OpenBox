// 技能管理 › 投稿审核 — the queue on the left, the submission on the right.
import { useTranslation } from "react-i18next"
import { useUrlState } from "@/shared/hooks/useUrlState"
import { useReviewQueue } from "@/features/admin-skills/api/admin-skills"
import { ReviewDetail } from "./ReviewDetail"
import { ReviewQueue } from "./ReviewQueue"

const LIMIT = 20
const DEFAULTS = { state: "pending", id: "", offset: "0" }
// `id` names the row that is open, not which rows the queue holds: opening the
// third submission on page three must leave the operator on page three, with
// the next one still a click away. Switching `state` is a real filter change
// and does reset the cursor.
const KEEP_PAGE = ["id"] as const

export function ReviewPage() {
  const { t } = useTranslation("admin-skills")
  const [values, setValues] = useUrlState(DEFAULTS, { keepPage: KEEP_PAGE })
  const offset = Number(values.offset) || 0
  const query = useReviewQueue({ state: values.state, offset, limit: LIMIT })

  return (
    <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
      <ReviewQueue
        state={values.state}
        onState={(state) => setValues({ state, id: "" })}
        rows={query.data?.items ?? []}
        isLoading={query.isPending}
        error={query.error}
        selectedId={values.id}
        onSelect={(id) => setValues({ id })}
        offset={offset}
        limit={LIMIT}
        total={query.data?.total ?? 0}
        onOffsetChange={(next) => setValues({ offset: String(next) })}
      />
      {values.id ? (
        <ReviewDetail key={values.id} catalogId={values.id} />
      ) : (
        <section className="border-hair bg-card flex-1 rounded-xl border p-4">
          <p className="text-n600 py-16 text-center text-sm">{t("review.pick")}</p>
        </section>
      )}
    </div>
  )
}
