/// Product-facing notation for paths persisted by old and new workspace
/// layouts. Relative paths are already project-scoped and pass through.
String projectScopedDisplayPath(String path) {
  final namespaced = RegExp(
    r'^/workspace/openbox/users/[^/]+/projects/[^/]+(?:/(.*))?$',
  ).firstMatch(path);
  if (namespaced != null) return namespaced.group(1) ?? '.';

  final uploaded = RegExp(
    r'^/workspace/openbox/users/[^/]+/\.openbox/uploads/[^/]+(?:/(.*))?$',
  ).firstMatch(path);
  if (uploaded != null) {
    final rest = uploaded.group(1);
    return rest == null || rest.isEmpty
        ? '.openbox/uploads'
        : '.openbox/uploads/$rest';
  }

  final legacy = RegExp(r'^/workspace/[^/]+(?:/(.*))?$').firstMatch(path);
  if (legacy != null) return legacy.group(1) ?? '.';
  return path;
}

String projectScopedDisplayText(String text) {
  final physicalPath = RegExp(
    r'''/workspace/openbox/users/[^/\s;,"'<>]+/(?:projects/[^/\s;,"'<>]+|\.openbox/uploads/[^/\s;,"'<>]+)(?:/[^\n;,"'<>]*)?''',
  );
  return text
      .split('\n')
      .map((line) {
        final direct = projectScopedDisplayPath(line);
        if (direct != line) return direct;
        return line.replaceAllMapped(
          physicalPath,
          (match) => projectScopedDisplayPath(match.group(0)!),
        );
      })
      .join('\n');
}

const _toolPathPrefixes = [
  '*** Update File: ',
  '*** Add File: ',
  '*** Delete File: ',
  'Updated ',
  'Added ',
  'Deleted ',
  'Error on ',
];

/// Rewrites only path-bearing tool protocol lines, never file or PTY text.
String projectScopedToolText(String text) => text
    .split('\n')
    .map((line) {
      for (final prefix in _toolPathPrefixes) {
        if (line.startsWith(prefix)) {
          return '$prefix${projectScopedDisplayPath(line.substring(prefix.length))}';
        }
      }
      return line;
    })
    .join('\n');
