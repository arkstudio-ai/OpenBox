// Query keys carry the user id (ENGINEERING_SPEC §7.2). A store belongs to a
// workspace, which the http layer selects via header, so the workspace id is
// part of the key too or a switch would show the previous workspace's store.
export const storeKeys = {
  all: (userId: string) => ["store", userId] as const,
  list: (userId: string, workspaceId: string) => ["store", userId, "list", workspaceId] as const,
  starterCards: (userId: string, workspaceId: string, storeId: string, locale: string) =>
    ["store", userId, "starter-cards", workspaceId, storeId, locale] as const,
}
