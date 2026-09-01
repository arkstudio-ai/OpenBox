/** Product-facing notation for paths persisted by old/new workspace layouts. */
export function projectScopedDisplayPath(path: string): string {
  const namespaced = path.match(
    /^\/workspace\/openbox\/users\/[^/]+\/projects\/[^/]+(?:\/(.*))?$/,
  );
  if (namespaced) return namespaced[1] || ".";
  const uploaded = path.match(
    /^\/workspace\/openbox\/users\/[^/]+\/\.openbox\/uploads\/[^/]+(?:\/(.*))?$/,
  );
  if (uploaded)
    return uploaded[1] ? `.openbox/uploads/${uploaded[1]}` : ".openbox/uploads";
  const legacy = path.match(/^\/workspace\/[^/]+(?:\/(.*))?$/);
  if (legacy) return legacy[1] || ".";
  return path;
}

export function projectScopedDisplayText(text: string): string {
  const physicalPath =
    /\/workspace\/openbox\/users\/[^/\s;,"'<>]+\/(?:projects\/[^/\s;,"'<>]+|\.openbox\/uploads\/[^/\s;,"'<>]+)(?:\/[^\n;,"'<>]*)?/g;
  return text
    .split("\n")
    .map((line) => {
      const direct = projectScopedDisplayPath(line);
      return direct !== line
        ? direct
        : line.replace(physicalPath, (value) => projectScopedDisplayPath(value));
    })
    .join("\n");
}

const TOOL_PATH_PREFIXES = [
  "*** Update File: ",
  "*** Add File: ",
  "*** Delete File: ",
  "Updated ",
  "Added ",
  "Deleted ",
  "Error on ",
];

/** Rewrite only path-bearing tool protocol lines, never arbitrary file/PTY text. */
export function projectScopedToolText(text: string): string {
  return text
    .split("\n")
    .map((line) => {
      const prefix = TOOL_PATH_PREFIXES.find((candidate) =>
        line.startsWith(candidate),
      );
      return prefix
        ? `${prefix}${projectScopedDisplayPath(line.slice(prefix.length))}`
        : line;
    })
    .join("\n");
}
