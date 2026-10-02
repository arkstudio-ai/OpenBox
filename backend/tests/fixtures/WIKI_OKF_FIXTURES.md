# OKF interoperability fixtures

Reference: `atomicstrata/llm-wiki-compiler`, commit
`eb18e769db8096c80b81665fb7195e3b29f4cb6b` (`1.4.0-rc.3`, MIT).

- `wiki_okf/` is an independently authored OKF 0.1 bundle. It includes unknown
  producer fields, a non-default page type and foreign workflow metadata to test
  preservation without granting local execution authority.
- `wiki_okf_upstream/` is output from the unmodified upstream `renderOkfDoc`,
  `buildOkfIndex`, `buildOkfLog` and `safeRefName` functions. The two handbook
  paragraphs are synthetic test content; no user or production data is included.
  It was generated on 2026-10-02. A release page links to the decision page and
  both cite the same reference file.

The upstream renderer computes its producer hash before replacing a Wiki link
with a Markdown path. Accordingly, the release page has a hash warning; the
decision page does not. The local importer preserves this warning and requires
explicit review, rather than silently trusting or discarding the producer hash.

`test_wiki_exchange.py` checks both fixtures. Live acceptance also imported the
upstream output through the browser, then fed the browser-downloaded OpenBox
bundle into upstream `readOkfBundle` and `okfDocToPage`. Five knowledge pages were
recognized; the six raw references were skipped as pages, as intended. Local
run evidence and reproduction entry points are listed in
`docs/WIKI_PLATFORM_IMPLEMENTATION.md`.
