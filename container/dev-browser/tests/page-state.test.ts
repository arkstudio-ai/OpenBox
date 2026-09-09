import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LocalPageState } from "../src/page-state.js";

test("relay restart preserves identities, Chrome restart invalidates them, endpoints stay isolated", () => {
  const dir = mkdtempSync(join(tmpdir(), "obx-pages-"));
  try {
    const first = new LocalPageState("http://localhost:9333", dir);
    first.restore("ws://localhost:9333/browser/one");
    first.pages.set("laike", "target-1"); first.save();
    const second = new LocalPageState("http://localhost:9333", dir);
    second.restore("ws://localhost:9333/browser/one");
    assert.equal(second.pages.get("laike"), "target-1");
    const other = new LocalPageState("http://localhost:9444", dir);
    other.restore("ws://localhost:9444/browser/one");
    assert.equal(other.pages.size, 0);
    second.restore("ws://localhost:9333/browser/two");
    assert.equal(second.pages.size, 0);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
