/// Reading real-person authorisations back out of a turn's tool calls — a
/// 1:1 port of frontend-v2 `features/chat/lib/video-identity-card.ts`.
///
/// The `video_identity` tool records the whole state on its own part, so the
/// card is rebuilt from the transcript rather than fetched: an old
/// conversation shows exactly what it showed at the time.
library;

import '../../../shared/models/message_part.dart';
import '../../../shared/models/video_identity.dart';

class IdentityCardData {
  const IdentityCardData({required this.identity, this.material});

  final VideoIdentity identity;
  final VideoMaterialAsset? material;
}

/// One card per identity, newest record winning. A later call may carry only
/// the identity, so the material already seen is carried forward rather than
/// dropped.
List<IdentityCardData> cardsFromTools(List<ToolPart> tools) {
  final byId = <String, IdentityCardData>{};
  for (final tool in tools) {
    if (tool.tool != 'video_identity') continue;
    final metadata = tool.metadata;

    final direct = VideoIdentity.tryParse(metadata['identity']);
    if (direct != null) {
      final previous = byId[direct.identityId];
      byId[direct.identityId] = IdentityCardData(
        identity: direct,
        material: VideoMaterialAsset.tryParse(metadata['material_asset']) ??
            previous?.material,
      );
    }

    final many = metadata['identities'];
    if (many is List) {
      for (final raw in many) {
        final identity = VideoIdentity.tryParse(raw);
        if (identity != null) {
          byId[identity.identityId] = IdentityCardData(identity: identity);
        }
      }
    }
  }
  return byId.values.toList();
}
