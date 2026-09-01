import assert from "node:assert/strict"
import { test } from "node:test"

import {
  authorizePreviewNavigation,
  type PreviewAccessResponse,
} from "./previewAccess.ts"

test("authorizes with the management endpoint before navigating", async () => {
  const events: string[] = []
  let resolveAccess: ((response: PreviewAccessResponse) => void) | undefined
  const accessResponse = new Promise<PreviewAccessResponse>((resolve) => {
    resolveAccess = resolve
  })

  const navigation = authorizePreviewNavigation(
    (containerId, port) => {
      events.push(`authorize:${containerId}:${port}`)
      return accessResponse
    },
    "container-1",
    3000,
    (preview) => events.push(`navigate:${preview.url}:${preview.isolated}`),
  )

  await Promise.resolve()
  assert.deepEqual(events, ["authorize:container-1:3000"])

  assert.ok(resolveAccess)
  resolveAccess({
    url: "/api/containers/container-1/preview/3000/",
    mode: "sandboxed_same_origin",
  })

  assert.deepEqual(await navigation, {
    url: "/api/containers/container-1/preview/3000/",
    isolated: false,
  })
  assert.deepEqual(events, [
    "authorize:container-1:3000",
    "navigate:/api/containers/container-1/preview/3000/:false",
  ])
})

test("rejects non-contract preview URLs without navigating", async (t) => {
  const invalidUrls = {
    "absolute external URL": "https://example.invalid/api/containers/container-1/preview/3000/",
    "protocol-relative external URL": "//example.invalid/api/containers/container-1/preview/3000/",
    "wrong container": "/api/containers/container-2/preview/3000/",
    "wrong port": "/api/containers/container-1/preview/4000/",
    "legacy token query": "/api/containers/container-1/preview/3000/?_pt=secret",
    "other query": "/api/containers/container-1/preview/3000/?theme=dark",
    "URL fragment": "/api/containers/container-1/preview/3000/#secret",
  }

  for (const [name, url] of Object.entries(invalidUrls)) {
    await t.test(name, async () => {
      let navigated = false
      await assert.rejects(
        authorizePreviewNavigation(
          async () => ({ url, mode: "sandboxed_same_origin" }),
          "container-1",
          3000,
          () => {
            navigated = true
          },
        ),
        /non-contract URL/,
      )
      assert.equal(navigated, false)
    })
  }
})

test("attests a distinct HTTPS preview origin before enabling full navigation", async () => {
  const navigations: unknown[] = []
  const result = await authorizePreviewNavigation(
    async () => ({
      url: "https://preview.example.test/api/containers/container-1/preview/3000/",
      mode: "isolated_origin",
    }),
    "container-1",
    3000,
    (preview) => navigations.push(preview),
  )

  assert.deepEqual(result, {
    url: "https://preview.example.test/api/containers/container-1/preview/3000/",
    isolated: true,
  })
  assert.deepEqual(navigations, [result])
})
