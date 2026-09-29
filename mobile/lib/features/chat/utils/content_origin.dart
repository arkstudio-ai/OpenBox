import '../../../shared/models/message_part.dart';

/// Agent attachments also include originals, QR codes and screenshots.
/// A verified artifact projection also retains the origin of a share_file copy.
/// A final role or a large preview by itself is not generation provenance.
bool isGeneratedMedia(FilePart part, {String? artifactKind}) {
  if (part.transient || part.relation?.role == 'evidence') return false;
  const generatedKinds = {
    'generated_image',
    'video_segment',
    'video_final',
    'generated_audio',
  };
  return generatedKinds.contains(part.relation?.kind) ||
      generatedKinds.contains(artifactKind);
}
