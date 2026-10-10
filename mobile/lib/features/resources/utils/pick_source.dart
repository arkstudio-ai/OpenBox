import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// Pick files for upload, asking where from first.
///
/// `FileType.any` opens the system *document* picker. On iOS that is the
/// Files app, which cannot see the Photos library at all. On stock Android
/// the documents UI lists the gallery in its drawer, but Xiaomi, Huawei,
/// OPPO and vivo ROMs replace it with their own file manager, where the
/// album root is hidden or missing.
///
/// iOS therefore offers the album (`FileType.media`, the `PHPicker`) next to
/// the Files app.
///
/// Android cannot use `FileType.media`: the plugin sends that as an
/// `ACTION_GET_CONTENT` whose primary type is `*/*` with image/video only
/// in the extra MIME list. Gallery apps register for `image/*` and
/// `video/*`, so they never match, and the only taker is the file manager,
/// which just opens on its pictures folder. `FileType.image` and
/// `FileType.video` send `image/*` / `video/*` directly, which every
/// gallery app answers — hence two entries, photos and videos, plus files.
///
/// Returns an empty list when the user backs out at either step.
Future<List<PlatformFile>> pickUploadFiles(
  BuildContext context,
  WidgetRef ref,
) async {
  final type = await _showSourceSheet(context, ref);
  if (type == null) return const [];
  return FilePickerPlatform.instance.pickFiles(type: type);
}

class _Source {
  const _Source(this.type, this.icon, this.labelKey);

  final FileType type;
  final IconData icon;
  final String labelKey;
}

List<_Source> _sourcesFor(TargetPlatform platform) => switch (platform) {
      TargetPlatform.android => const [
          _Source(FileType.image, Icons.photo_library_outlined,
              'resources:upload.fromPhotos'),
          _Source(FileType.video, Icons.videocam_outlined,
              'resources:upload.fromVideos'),
          _Source(FileType.any, Icons.folder_open_outlined,
              'resources:upload.fromFiles'),
        ],
      _ => const [
          _Source(FileType.media, Icons.photo_library_outlined,
              'resources:upload.fromAlbum'),
          _Source(FileType.any, Icons.folder_open_outlined,
              'resources:upload.fromFiles'),
        ],
    };

Future<FileType?> _showSourceSheet(BuildContext context, WidgetRef ref) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final sources = _sourcesFor(defaultTargetPlatform);
  return showModalBottomSheet<FileType>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
            child: Text(
              i18n.t('resources:upload.source'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n600,
              ),
            ),
          ),
          for (final source in sources)
            ListTile(
              dense: true,
              leading: Icon(source.icon, size: 18, color: t.n600),
              title: Text(
                i18n.t(source.labelKey),
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
              onTap: () => Navigator.pop(sheetContext, source.type),
            ),
        ],
      ),
    ),
  );
}
