// What the composer needs to know about a resource-centre pick.
//
// Deliberately declared here rather than imported from the resources feature:
// features never reach across to each other (§4.1). The workspace routes hand
// the real objects in, and structural typing does the rest.
export interface AttachableResource {
  id: string
  name: string
  mime: string
  size: number
  kind: string
  /** Where the file lands in the sandbox once the message is sent. */
  sandboxPath: string
  /** Presigned GET, used for the thumbnail in the composer strip. */
  url: string
}
