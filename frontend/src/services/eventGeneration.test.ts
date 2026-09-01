import assert from "node:assert/strict"
import { test } from "node:test"
import { EventGenerationGate } from "./eventGeneration.ts"

test("EventGenerationGate rejects old terminal state after newer busy", () => {
  const gate = new EventGenerationGate()

  assert.equal(gate.acceptStatus("session-1", 2, "busy"), true)
  assert.equal(gate.acceptStatus("session-1", 1, "idle"), false)
  assert.equal(gate.acceptStatus("session-1", undefined, "error"), false)
  assert.equal(gate.acceptStatus("session-1", 2, "idle"), true)
  assert.equal(gate.acceptStatus("session-1", 2, "finalizing"), false)
})
