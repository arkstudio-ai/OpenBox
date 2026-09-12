// Record table geometry shared by the header and every row.

export const ROW_HEIGHT = 30

/**
 * Column template driven by the width of the table itself (container
 * `records`), not the viewport: the table shares the screen with the agent
 * pane and the inspector. The record and its status are always shown;
 * duration, start, position and agent appear as room allows. The template for
 * each width lists exactly the cells visible at that width, in DOM order.
 */
export const ROW_GRID =
  "grid grid-cols-[minmax(0,1fr)_5.5rem] @min-[30rem]/records:grid-cols-[minmax(0,1fr)_5.5rem_4.5rem] @min-[40rem]/records:grid-cols-[minmax(0,1fr)_5.5rem_6.5rem_4.5rem] @min-[50rem]/records:grid-cols-[minmax(0,1fr)_5.5rem_7rem_6.5rem_4.5rem] @min-[62rem]/records:grid-cols-[minmax(0,1fr)_5.5rem_7rem_8rem_6.5rem_4.5rem]"

export const COLUMN_VISIBILITY = {
  position: "hidden @min-[50rem]/records:block",
  agent: "hidden @min-[62rem]/records:block",
  started: "hidden @min-[40rem]/records:block",
  duration: "hidden @min-[30rem]/records:block",
} as const
