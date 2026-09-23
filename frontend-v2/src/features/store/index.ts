// Public surface of the store feature.
//   - StoreSetupDialog: first-run 「你的店」 form (mount in the workspace layout).
//   - useStoreEvents: mount once so `store.updated` refreshes store queries.
//   - useStarterSuggestions: cards for the empty chat page, or null.
export { StoreSetupDialog } from "./components/StoreSetupDialog"
export { useStoreEvents } from "./hooks/useStoreEvents"
export {
  useCreateStore,
  useCurrentStore,
  useRegeneratePersona,
  useStarterCards,
  useStarterSuggestions,
  useStoreQuery,
  useUpdateStore,
} from "./api/hooks"
export type { StarterCard, Store, StoreCategory, StoreList, StorePlatform } from "./types"
