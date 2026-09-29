import { mkdir, readFile, writeFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const escape = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  )
const docIds = [
  "terms",
  "privacy",
  "ai",
  "disclaimer",
  "contact",
  "collection",
  "third-parties",
  "permissions",
]

/** Real static pages: reading policies never boots authentication or a third-party SDK. */
export async function buildLegal(output = path.join(root, "public/legal")) {
  await mkdir(output, { recursive: true })
  await writeFile(path.join(output, "site.css"), await readFile(path.join(root, "scripts/legal.css")))
  for (const language of ["zh-CN", "en-US"]) {
    const copy = JSON.parse(await readFile(path.join(root, `src/locales/${language}/legal.json`), "utf8"))
    const prefix = language === "zh-CN" ? "" : "en/"
    const url = (slug = "") => `/legal/${prefix}${slug ? `${slug}/` : ""}`
    for (const id of [null, ...docIds]) {
      const doc = id ? copy.documents[id] : null
      const title = doc?.title ?? copy.title
      const links = docIds
        .map(
          (key) =>
            `<a ${key === id ? 'aria-current="page"' : ""} href="${url(copy.documents[key].slug)}">${escape(copy.documents[key].title)}</a>`,
        )
        .join("")
      const content = doc
        ? doc.sections
            .map(
              (s) =>
                `<section id="${escape(s.id)}"><h2>${escape(s.title)}</h2>${s.paragraphs.map((p) => `<p>${escape(p)}</p>`).join("")}</section>`,
            )
            .join("")
        : `<div class="cards">${docIds.map((key) => `<a class="card" href="${url(copy.documents[key].slug)}"><h2>${escape(copy.documents[key].title)}</h2><p>${escape(copy.documents[key].summary)}</p><span aria-hidden="true">↗</span></a>`).join("")}</div>`
      const email = (subject) =>
        `mailto:${copy.contactEmail}?subject=${encodeURIComponent(`BossIP ${subject}`)}`
      const contact =
        id === "contact"
          ? `<div class="actions">${["contactAction", "privacyAction", "reportAction"].map((key) => `<a href="${escape(email(copy[key]))}">${escape(copy[key])}</a>`).join("")}</div><p>${escape(copy.contactEmail)}</p><p class="muted">${escape(copy.emailHint)}</p>`
          : ""
      const providers =
        id === "third-parties"
          ? copy.providerLinks
              .map(
                (l) =>
                  `<p><a href="${escape(l.url)}" target="_blank" rel="noopener noreferrer">${escape(l.label)} ↗</a></p>`,
              )
              .join("")
          : ""
      const other = `/legal/${language === "zh-CN" ? "en/" : ""}${doc ? `${doc.slug}/` : ""}`
      const html = `<!doctype html><html lang="${language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escape(title)} · BossIP</title><meta name="description" content="${escape(doc?.summary ?? copy.intro)}"><link rel="canonical" href="${copy.publicOrigin}${url(doc?.slug)}"><link rel="stylesheet" href="/legal/site.css"></head><body>
<header><a class="brand" href="/">${escape(copy.brand)}</a><a href="/">${escape(copy.home)}</a><a lang="${language === "zh-CN" ? "en" : "zh-CN"}" href="${other}">${escape(copy.language)}</a></header>
<div class="layout"><aside><a class="center-link" href="${url()}">${escape(copy.title)}</a><nav aria-label="${escape(copy.directory)}">${links}</nav></aside>
<main id="content"><div class="eyebrow">${escape(copy.publicAccess)}</div><h1>${escape(title)}</h1><p class="lede">${escape(doc?.summary ?? copy.intro)}</p><div class="meta">${escape(copy.updated)} ${copy.updatedAt} <span>·</span> ${escape(copy.versionLabel)} ${copy.version}</div>${content}${contact}${providers}</main></div>
<footer><p>${escape(copy.operator)}</p><a href="${copy.icpUrl}" target="_blank" rel="noopener noreferrer">${escape(copy.siteIcp)}</a><span> · </span><a href="${url("contact")}">${escape(copy.documents.contact.title)}</a></footer></body></html>`
      const directory = path.join(output, prefix, doc?.slug ?? "")
      await mkdir(directory, { recursive: true })
      await writeFile(path.join(directory, "index.html"), html)
    }
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await buildLegal()
