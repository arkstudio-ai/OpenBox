import { useTranslation } from "react-i18next"
import { assistantNames } from "./assistant-profile"
import { useAppearanceStore } from "./store"

/** The assistant's name for a component (assistantNames): follows the account's profile, so a
 *  rename made anywhere shows at once. */
export function useAssistantNames() {
  const { t } = useTranslation("common")
  const custom = useAppearanceStore((s) => s.assistant.name)
  return assistantNames(custom, t)
}
