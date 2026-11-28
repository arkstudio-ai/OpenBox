import { useSearchParams } from "react-router"
import { ExampleSheet, TaxonomySheet, useExample, type SheetMode } from "@/features/admin-examples"
import { AdminMediaLibrary } from "@/features/media-templates"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"

function LegacyExampleEditor({
  exampleId,
  onClose,
  onSaved,
}: {
  exampleId: string | null
  onClose: () => void
  onSaved: () => void
}) {
  const [params, setParams] = useSearchParams()
  const collect = params.get("collect")
  const example = useExample(exampleId)
  const errorMessage = useApiErrorMessage()
  const mode: SheetMode | null = collect
    ? { kind: "collect", sessionId: collect, assetId: params.get("asset") }
    : example.data && exampleId
      ? { kind: "edit", example: example.data }
      : null
  if (example.error) return <p role="alert" className="text-dangerink">{errorMessage(example.error)}</p>
  if (!mode) return null
  return (
    <ExampleSheet
      mode={mode}
      onClose={() => {
        if (collect) setParams({}, { replace: true })
        onClose()
      }}
      onSaved={onSaved}
    />
  )
}

export default function AdminExamplesRoute() {
  return <AdminMediaLibrary LegacyExampleEditor={LegacyExampleEditor} TaxonomyEditor={TaxonomySheet} />
}
