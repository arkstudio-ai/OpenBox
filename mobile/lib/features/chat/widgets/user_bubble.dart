import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import 'attachment_gallery.dart';

const _attachmentsMarker = '\n\n[attachments]\n';

/// Splits the `[attachments]` trailer off a user message
/// (web `UserBubble.tsx:9-31`).
(String, List<String>) splitAttachments(String text) {
  final index = text.indexOf(_attachmentsMarker);
  if (index == -1) return (text, const []);
  final body = text.substring(0, index);
  final paths = text
      .substring(index + _attachmentsMarker.length)
      .split('\n')
      .map((line) => line.startsWith('- ') ? line.substring(2).trim() : line.trim())
      .where((line) => line.isNotEmpty)
      .toList();
  return (body, paths);
}

/// Right-aligned user bubble (web `UserBubble`): max 88% width, muted fill,
/// height clamp with expand/collapse, attachment chips.
class UserBubble extends ConsumerStatefulWidget {
  const UserBubble({super.key, required this.message});

  final ChatMessage message;

  @override
  ConsumerState<UserBubble> createState() => _UserBubbleState();
}

class _UserBubbleState extends ConsumerState<UserBubble> {
  bool _expanded = false;

  static const _clampHeight = 128.0;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final rawText = widget.message.parts
        .whereType<TextPart>()
        .map((p) => p.text)
        .join('\n\n');
    final (text, legacyFiles) = splitAttachments(rawText);
    final fileParts = widget.message.parts.whereType<FilePart>().toList();
    // Media previews come from proper file parts (asset ids); the text
    // trailer is only the fallback for pre-OSS messages (web parity).
    final mediaParts = fileParts.where(isGalleryMedia).toList();
    final chips = fileParts.isNotEmpty
        ? fileParts
            .where((f) => !isGalleryMedia(f))
            .map((f) => f.path.split('/').last)
            .toList()
        : legacyFiles.map((p) => p.split('/').last).toList();

    final needsClamp = !_expanded && text.length > 360;

    return Align(
      alignment: Alignment.centerRight,
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.sizeOf(context).width * 0.88,
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(
                color: t.n200.withValues(alpha: 0.6),
                borderRadius: BorderRadius.circular(Radii.xl),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  ConstrainedBox(
                    constraints: needsClamp
                        ? const BoxConstraints(maxHeight: _clampHeight)
                        : const BoxConstraints(),
                    child: Text(
                      text,
                      overflow: needsClamp ? TextOverflow.fade : null,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        height: 1.65,
                        color: t.ink,
                      ),
                    ),
                  ),
                  if (text.length > 360)
                    GestureDetector(
                      onTap: () => setState(() => _expanded = !_expanded),
                      child: Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Text(
                          _expanded
                              ? i18n.t('chat:meta.collapseMessage')
                              : i18n.t('chat:meta.expandMessage'),
                          style:
                              TextStyle(fontSize: FontSizes.xs, color: t.n600),
                        ),
                      ),
                    ),
                ],
              ),
            ),
            if (mediaParts.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: AttachmentGallery(parts: mediaParts, alignEnd: true),
              ),
            if (chips.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Wrap(
                  alignment: WrapAlignment.end,
                  spacing: 6,
                  runSpacing: 6,
                  children: [
                    for (final name in chips) FileChipRow(name: name),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}
