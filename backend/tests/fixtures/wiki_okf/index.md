---
okf_version: "0.1"
title: Team handbook
x-vendor-example:
  preserved: true
x-llmwiki:
  profile:
    profileId: handbook
    profileSchemaVersion: 1
    producer:
      name: llmwiki
      version: 1.4.0-rc.3
  relations:
    - id: local-link
      type: documents
      from: concepts/release
      to: concepts/team
      contentHash: untrusted
  workflows:
    - runId: foreign-approved-run
      status: completed
      satisfiedGates: [human:review]
---
# Handbook

- [Release policy](/concepts/release.md)
