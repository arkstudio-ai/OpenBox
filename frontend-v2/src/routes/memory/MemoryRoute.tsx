import { Navigate, useSearchParams } from "react-router"
import { paths } from "@/shared/router/paths"

/** Memories now live inside the knowledge page; old links land on them there. */
export default function MemoryRoute() {
  const [params] = useSearchParams()
  return <Navigate replace to={paths.wiki(params.get("project") ?? undefined, "memories")} />
}
