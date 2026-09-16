import 'package:flutter/material.dart';
import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/models/resource.dart';
import '../../../../shared/utils/format.dart';

class AttachmentStrip extends StatelessWidget {
  const AttachmentStrip({
    super.key,
    required this.attachments,
    required this.uploading,
    required this.onRemove,
  });

  final List<Resource> attachments;
  final bool uploading;
  final ValueChanged<Resource> onRemove;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return SizedBox(
      height: 56,
      child: ListView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.fromLTRB(12, 10, 12, 0),
        children: [
          if (uploading)
            Container(
              width: 56,
              margin: const EdgeInsets.only(right: 8),
              decoration: BoxDecoration(
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.lg),
              ),
              child: const Center(
                child: SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              ),
            ),
          for (final resource in attachments)
            Container(
              margin: const EdgeInsets.only(right: 8),
              padding: const EdgeInsets.fromLTRB(8, 0, 4, 0),
              constraints: const BoxConstraints(maxWidth: 190),
              decoration: BoxDecoration(
                color: t.n200,
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.lg),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (resource.kind == 'image' && resource.url.isNotEmpty)
                    ClipRRect(
                      borderRadius: BorderRadius.circular(Radii.sm),
                      child: Image.network(
                        resource.url,
                        width: 26,
                        height: 26,
                        fit: BoxFit.cover,
                        errorBuilder: (_, _, _) =>
                            Icon(Icons.image_outlined, size: 15, color: t.n600),
                      ),
                    )
                  else
                    Icon(
                      Icons.insert_drive_file_outlined,
                      size: 15,
                      color: t.n600,
                    ),
                  const SizedBox(width: 7),
                  Flexible(
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          resource.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            fontWeight: FontWeight.w500,
                            color: t.ink,
                          ),
                        ),
                        Text(
                          formatBytes(resource.size),
                          style: TextStyle(
                            fontSize: FontSizes.xs2,
                            color: t.n600,
                          ),
                        ),
                      ],
                    ),
                  ),
                  IconButton(
                    onPressed: () => onRemove(resource),
                    icon: Icon(Icons.close, size: 13, color: t.n600),
                    visualDensity: VisualDensity.compact,
                    constraints: const BoxConstraints.tightFor(
                      width: 26,
                      height: 26,
                    ),
                    padding: EdgeInsets.zero,
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
