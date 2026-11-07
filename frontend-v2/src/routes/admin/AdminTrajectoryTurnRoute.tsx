import { useState } from "react"
import { useTranslation } from "react-i18next"
import { useParams } from "react-router"
import { ExampleSheet } from "@/features/admin-examples"
import { TrajectoryTurnPage, useTrajectoryAccessWatcher } from "@/features/admin-trajectories"
import { toast } from "@/shared/ui/Toast"

/** 轨迹监控 › 简明模式 › 轮详情 — one turn as a step flow. 收进样例库 opens the
 *  library's collect sheet over this page, so the reader keeps their place. */
export default function AdminTrajectoryTurnRoute() {
  useTrajectoryAccessWatcher()
  const { t } = useTranslation("admin-examples")
  const { userId = "", sessionId = "", turnId = "" } = useParams()
  const [collecting, setCollecting] = useState(false)
  return (
    <>
      <TrajectoryTurnPage userId={userId} sessionId={sessionId} turnId={turnId} onCollectExample={() => setCollecting(true)} />
      <ExampleSheet
        mode={collecting ? { kind: "collect", sessionId } : null}
        onClose={() => setCollecting(false)}
        onSaved={() => toast("success", t("form.saved"))}
      />
    </>
  )
}
