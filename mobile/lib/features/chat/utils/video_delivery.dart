part of 'content_view.dart';

bool _uploadedVideo(FilePart part) =>
    (part.assetId?.trim().isNotEmpty ?? false) &&
    (part.mimeType?.startsWith('video/') ?? false);

bool isFinalVideoArtifact(ArtifactGroup group) =>
    group.artifactKind != 'video_segment' &&
    (group.artifactKind == 'video_final' || group.role == 'final') &&
    group.parts.any(_uploadedVideo);

bool _standaloneSegment(ArtifactGroup group) {
  final tool = group.sourceTool;
  if (tool?.tool != 'video_generate' || tool?.status != ToolStatus.completed) {
    return false;
  }
  if (_outputValue(_toolOutput(tool), 'status') != 'completed') return false;
  for (final key in ['production_id', 'segment_id']) {
    if (group.metadataString(key) != null ||
        _toolInputValue(tool, key) != null ||
        _outputValue(_toolOutput(tool), key) != null) {
      return false;
    }
  }
  return group.parts.any(_uploadedVideo);
}

bool _deliveredCopy(ArtifactGroup group, ArtifactGroup segment) {
  if (group.artifactKind != 'shared_file' ||
      group.role != 'result' ||
      group.order <= segment.order) {
    return false;
  }
  final tool = group.sourceTool;
  final input = tool?.input;
  if (tool?.tool != 'share_file' ||
      tool?.status != ToolStatus.completed ||
      tool?.metadata['attached'] == false ||
      (input is Map<String, dynamic> && input['attach'] == false)) {
    return false;
  }
  if (group.parts.isEmpty || !group.parts.every(_uploadedVideo)) return false;
  final assets = segment.parts
      .where(_uploadedVideo)
      .map((p) => p.assetId)
      .toSet();
  final workspacePath = _outputValue(
    _toolOutput(segment.sourceTool),
    'workspace_path',
  );
  // Match persisted provenance, not the filename. Re-sharing can create a new asset ID.
  return group.parts.every(
    (p) =>
        assets.contains(p.assetId) ||
        (workspacePath != null && p.path == workspacePath),
  );
}

/// Mirror Web's compatibility projection. Ordinary previews stay ordinary;
/// only an unambiguous standalone output shared in a completed turn is promoted.
/// Stored parts, assets and source relationships are never rewritten.
List<ArtifactGroup> _resolveDirectVideoDelivery(
  List<ArtifactGroup> groups,
  bool completed,
) {
  if (!completed || groups.any(isFinalVideoArtifact)) return groups;
  final segments = groups
      .where((g) => g.artifactKind == 'video_segment')
      .toList();
  if (segments.length != 1 || !_standaloneSegment(segments.single)) {
    return groups;
  }
  ArtifactGroup? delivery;
  for (final group in groups) {
    if (_deliveredCopy(group, segments.single)) delivery = group;
  }
  return [
    for (final group in groups)
      if (identical(group, delivery))
        ArtifactGroup(
          id: group.id,
          order: group.order,
          artifactKind: 'video_final',
          role: 'final',
          label: group.label,
          caption: group.caption,
          ordinal: group.ordinal,
          revision: group.revision,
          metadata: group.metadata,
          sourceTool: group.sourceTool,
          parts: group.parts,
        )
      else
        group,
  ];
}
