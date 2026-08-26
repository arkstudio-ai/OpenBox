import 'package:flutter/widgets.dart';

import '../../../../shared/models/resource.dart';

/// How the composer reaches the resource centre without importing it.
///
/// Features never import each other (mobile README §分层), so the chat side
/// declares the shape it needs — over `shared/models` types only — and the app
/// layer fills it in with the resources feature's widgets. This is the same
/// composition the web does in `routes/workspace/*`.
class ComposerResourceSlot {
  const ComposerResourceSlot({
    required this.mentionSection,
    required this.pickAndUpload,
  });

  /// The resource block of the "@" menu: its own project/source switcher plus
  /// the rows matching [query]. Opens on [projectId] — the conversation's own
  /// project — and lets the person step out of it.
  final Widget Function(
    BuildContext context, {
    required String query,
    required String? projectId,
    required ValueChanged<Resource> onPick,
  }) mentionSection;

  /// Picks files off the device, uploads them into [projectId], and returns
  /// what landed. Empty when the person cancelled or every upload failed.
  final Future<List<Resource>> Function(
    BuildContext context, {
    required String? projectId,
  }) pickAndUpload;
}
