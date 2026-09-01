// POSIX path helpers for the project-scoped file browser.
//
// API calls still use the physical WUYING path. UI text and clipboard values
// must never expose that namespaced path; they are derived relative to the
// exact project root instead.

String normalizeWorkspacePath(String path) {
  if (path.isEmpty) return '';
  final absolute = path.startsWith('/');
  final parts = <String>[];
  for (final part in path.split('/')) {
    if (part.isEmpty || part == '.') continue;
    if (part == '..') {
      if (parts.isNotEmpty && parts.last != '..') {
        parts.removeLast();
      } else if (!absolute) {
        parts.add(part);
      }
      continue;
    }
    parts.add(part);
  }
  final normalized = parts.join('/');
  if (!absolute) return normalized;
  return normalized.isEmpty ? '/' : '/$normalized';
}

bool isWithinProjectRoot(String root, String path) {
  final normalizedRoot = normalizeWorkspacePath(root);
  final normalizedPath = normalizeWorkspacePath(path);
  if (!normalizedRoot.startsWith('/') || !normalizedPath.startsWith('/')) {
    return false;
  }
  if (normalizedRoot == '/') return true;
  return normalizedPath == normalizedRoot ||
      normalizedPath.startsWith('$normalizedRoot/');
}

/// Returns a project-relative path, `.` for the root, or null when [path]
/// escapes the project.
String? projectRelativePath(String root, String path) {
  final normalizedRoot = normalizeWorkspacePath(root);
  final normalizedPath = normalizeWorkspacePath(path);
  if (!isWithinProjectRoot(normalizedRoot, normalizedPath)) return null;
  if (normalizedPath == normalizedRoot) return '.';
  return normalizedPath.substring(normalizedRoot.length + 1);
}

/// Human-facing breadcrumb rooted at the user-visible project name.
String projectDisplayPath({
  required String root,
  required String path,
  required String projectName,
}) {
  final relative = projectRelativePath(root, path);
  if (relative == null || relative == '.') return projectName;
  return '$projectName/$relative';
}

/// Resolves one API file entry while rejecting paths outside [root].
/// Returned paths remain absolute and are only used for subsequent API calls.
String? resolveProjectEntryPath({
  required String root,
  required String cwd,
  required String entryPath,
  required String entryName,
}) {
  if (!isWithinProjectRoot(root, cwd)) return null;
  final candidate = entryPath.isNotEmpty
      ? (entryPath.startsWith('/') ? entryPath : '$cwd/$entryPath')
      : '$cwd/$entryName';
  final normalized = normalizeWorkspacePath(candidate);
  return isWithinProjectRoot(root, normalized) ? normalized : null;
}

String projectParentPath(String root, String cwd) {
  final normalizedRoot = normalizeWorkspacePath(root);
  final normalizedCwd = normalizeWorkspacePath(cwd);
  if (!isWithinProjectRoot(normalizedRoot, normalizedCwd) ||
      normalizedCwd == normalizedRoot) {
    return normalizedRoot;
  }
  final slash = normalizedCwd.lastIndexOf('/');
  final parent = slash <= 0
      ? normalizedRoot
      : normalizedCwd.substring(0, slash);
  return isWithinProjectRoot(normalizedRoot, parent) ? parent : normalizedRoot;
}
