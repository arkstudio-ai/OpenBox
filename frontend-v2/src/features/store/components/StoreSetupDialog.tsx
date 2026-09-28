// First-run 「你的店」 form (docs/OPS_CASE_PLAN.md §2.1): the first time a
// workspace opens with no store on file, ask for the shop's name, trade and
// main platforms so the starter cards and the agent's work can be about it.
//
// Skippable: 稍后 remembers the skip per workspace in localStorage, and the
// ops centre keeps a "register your store" card for later. It never shows on
// a guess — only once the store list has loaded empty.
import { useCallback, useState, type FormEvent } from "react"
import { useTranslation } from "react-i18next"
import { ApiError } from "@/shared/api/http"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useApiErrorMessage } from "@/shared/hooks/useApiErrorMessage"
import { Dialog, DialogActions, DialogBody, DialogTitle } from "@/shared/ui/Dialog"
import { useCreateStore, useStoreQuery } from "../api/hooks"
import { readStoreSetupDismissed, writeStoreSetupDismissed } from "../lib/setupDismissed"
import type { StoreCategory, StorePlatform } from "../types"

const CATEGORIES: readonly StoreCategory[] = ["food", "beauty", "retail", "other"]
const PLATFORMS: readonly StorePlatform[] = ["douyin_laike", "meituan_merchant"]

export function StoreSetupDialog() {
  const workspaceId = useWorkspaceStore((s) => s.currentId)
  if (!workspaceId) return null
  // Switching workspaces must discard the form and the in-session skip.
  return <ScopedStoreSetupDialog key={workspaceId} workspaceId={workspaceId} />
}

function ScopedStoreSetupDialog({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation("store")
  const query = useStoreQuery()
  const create = useCreateStore()
  const describe = useApiErrorMessage()
  const [dismissed, setDismissed] = useState(() => readStoreSetupDismissed(workspaceId))
  const [name, setName] = useState("")
  const [category, setCategory] = useState<StoreCategory>("food")
  const [platforms, setPlatforms] = useState<StorePlatform[]>([])

  const items = query.data?.items
  const openCategories = query.data?.openCategories ?? []
  const catalogue = (query.data?.categories ?? CATEGORIES).filter((c): c is StoreCategory =>
    (CATEGORIES as readonly string[]).includes(c),
  )
  const platformCatalogue = (query.data?.platforms ?? PLATFORMS).filter((p): p is StorePlatform =>
    (PLATFORMS as readonly string[]).includes(p),
  )
  const open = !dismissed && Array.isArray(items) && items.length === 0

  const dismiss = useCallback(() => {
    writeStoreSetupDismissed(workspaceId)
    setDismissed(true)
  }, [workspaceId])

  const canSubmit = name.trim().length > 0 && openCategories.includes(category) && !create.isPending

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return
    create.mutate(
      { name: name.trim(), category, main_platforms: platforms },
      {
        onSuccess: () => setDismissed(true),
        onError: (error) => {
          // Someone else in the workspace got there first: nothing to add.
          if (error instanceof ApiError && error.code === "STORE_EXISTS") setDismissed(true)
        },
      },
    )
  }

  const togglePlatform = (platform: StorePlatform) =>
    setPlatforms((prev) => (prev.includes(platform) ? prev.filter((p) => p !== platform) : [...prev, platform]))

  return (
    <Dialog open={open} onClose={dismiss} label={t("setup.title")}>
      <form onSubmit={submit} className="flex flex-col gap-2.5" data-testid="store-setup-form">
        <DialogTitle>{t("setup.title")}</DialogTitle>
        <DialogBody>{t("setup.body")}</DialogBody>

        <label className="mt-2 block">
          <span className="text-n600 text-xs">{t("setup.name")}</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={80}
            required
            autoFocus
            disabled={create.isPending}
            placeholder={t("setup.namePlaceholder")}
            className="border-hair bg-bg text-ink placeholder:text-n600 focus:border-accent mt-1 w-full rounded-lg border px-2.5 py-2 text-sm outline-none disabled:opacity-50"
          />
        </label>

        <label className="mt-1 block">
          <span className="text-n600 text-xs">{t("setup.category")}</span>
          <select
            value={category}
            onChange={(event) => setCategory(event.target.value as StoreCategory)}
            disabled={create.isPending}
            className="border-hair bg-bg text-ink focus:border-accent mt-1 w-full rounded-lg border px-2.5 py-2 text-sm outline-none disabled:opacity-50"
          >
            {catalogue.map((c) => {
              const isOpen = openCategories.includes(c)
              const label = t(`setup.categories.${c}`)
              return (
                <option key={c} value={c} disabled={!isOpen}>
                  {isOpen ? label : t("setup.categoryComingSoon", { name: label })}
                </option>
              )
            })}
          </select>
        </label>

        <fieldset className="mt-1 flex flex-col gap-1.5" disabled={create.isPending}>
          <legend className="text-n600 text-xs">{t("setup.platforms")}</legend>
          {platformCatalogue.map((p) => (
            <label key={p} className="text-ink flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={platforms.includes(p)}
                onChange={() => togglePlatform(p)}
                className="accent-a700 size-4"
              />
              {t(`setup.platform.${p}`)}
            </label>
          ))}
        </fieldset>

        {create.isError && !(create.error instanceof ApiError && create.error.code === "STORE_EXISTS") && (
          <p role="alert" className="bg-dangersoft text-danger rounded-lg px-3 py-2 text-xs leading-5">
            {describe(create.error)}
          </p>
        )}

        <DialogActions>
          <button
            type="button"
            onClick={dismiss}
            disabled={create.isPending}
            className="text-n700 hover:bg-hairsoft rounded-full px-3.5 py-1.5 text-sm disabled:opacity-50"
          >
            {t("setup.later")}
          </button>
          <button
            type="submit"
            disabled={!canSubmit}
            className="bg-ink text-bg rounded-full px-3.5 py-1.5 text-sm hover:opacity-90 disabled:opacity-50"
          >
            {create.isPending ? t("setup.saving") : t("setup.save")}
          </button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
