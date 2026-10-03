// The knowledge page's own look: the same pill buttons and hairline cards as
// the skills centre, so the two consumer pages read as one product.

export const button =
  "inline-flex min-h-9 items-center justify-center gap-1.5 rounded-full border border-hair bg-card px-3.5 py-1.5 text-sm text-ink transition-colors hover:bg-hairsoft disabled:cursor-not-allowed disabled:opacity-50"
export const primaryButton =
  "inline-flex min-h-9 items-center justify-center gap-1.5 rounded-full bg-ink px-4 py-1.5 text-sm font-medium text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
// Soft rather than solid red: the solid danger tone has no light ink that stays
// legible on it in dark mode.
export const dangerButton =
  "inline-flex min-h-9 items-center justify-center gap-1.5 rounded-full bg-dangersoft px-4 py-1.5 text-sm font-medium text-dangerink transition-opacity hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-50"
export const textButton =
  "inline-flex items-center gap-1 rounded-full px-2 py-1 text-sm text-n700 transition-colors hover:bg-hairsoft hover:text-ink"
export const iconButton =
  "inline-flex size-8 flex-none items-center justify-center rounded-full text-n600 transition-colors hover:bg-hairsoft hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
export const field =
  "w-full rounded-xl border border-hair bg-card px-3 py-2 text-md text-ink outline-none transition-colors placeholder:text-n500 focus:border-accent"
export const card = "rounded-2xl border border-hair bg-card"
