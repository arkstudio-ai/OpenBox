import 'json.dart';

/// Mirrors `ContainerInfo` (frontend-v2 `shared/types/api.ts:288-295`).
class ContainerInfo {
  const ContainerInfo({
    required this.id,
    required this.name,
    required this.status,
    this.image,
    this.createdAt,
    this.port,
  });

  factory ContainerInfo.fromJson(Map<String, dynamic> json) => ContainerInfo(
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? '',
        status: asString(json['status']) ?? '',
        image: asString(json['image']),
        createdAt: asDate(json['created_at']),
        port: asInt(json['port']),
      );

  final String id;
  final String name;
  final String status;
  final String? image;
  final DateTime? createdAt;
  final int? port;

  bool get isRunning => status == 'running';
}

/// Mirrors `SandboxStatus` (`GET /api/agent/sandbox/status`).
class SandboxStatus {
  const SandboxStatus({
    required this.available,
    this.containerId,
    this.containerName,
    this.status,
  });

  factory SandboxStatus.fromJson(Map<String, dynamic> json) => SandboxStatus(
        available: asBool(json['available']) ?? false,
        containerId: asString(json['container_id']),
        containerName: asString(json['container_name']),
        status: asString(json['status']),
      );

  final bool available;
  final String? containerId;
  final String? containerName;
  final String? status;
}

/// A single entry from `POST /api/containers/{id}/files/list`. The shape
/// varies (`{files:[…]}` / `{entries:[…]}` / bare array) — normalized here
/// like web `workbench/api/files.ts:19-23`.
class FileEntry {
  const FileEntry({
    required this.name,
    required this.path,
    required this.isDir,
    this.size,
  });

  factory FileEntry.fromJson(Map<String, dynamic> json) => FileEntry(
        name: asString(json['name']) ?? '',
        path: asString(json['path']) ?? '',
        isDir: asBool(json['is_dir']) ?? asBool(json['isDir']) ?? false,
        size: asInt(json['size']),
      );

  static List<FileEntry> listFrom(dynamic body) {
    final raw = body is Map<String, dynamic>
        ? (body['files'] ?? body['entries'] ?? const <dynamic>[])
        : body;
    return asList(raw)
        .whereType<Map<String, dynamic>>()
        .map(FileEntry.fromJson)
        .toList();
  }

  final String name;
  final String path;
  final bool isDir;
  final int? size;
}

/// `GET .../files/content` response.
class FileContent {
  const FileContent({
    required this.path,
    required this.content,
    this.totalLines = 0,
    this.truncated = false,
  });

  factory FileContent.fromJson(Map<String, dynamic> json) => FileContent(
        path: asString(json['path']) ?? '',
        content: asString(json['content']) ?? '',
        totalLines: asInt(json['total_lines']) ?? 0,
        truncated: asBool(json['truncated']) ?? false,
      );

  final String path;
  final String content;
  final int totalLines;
  final bool truncated;
}
