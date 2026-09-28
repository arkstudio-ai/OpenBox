import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// Pick files for upload, asking where from when the platform needs it.
///
/// `FileType.any` opens the system *document* picker. On Android that
/// picker lists the gallery alongside Downloads and Drive, so one entry
/// covers everything. On iOS it is the Files app, which cannot see the
/// Photos library at all — photos and videos only come through the
/// `PHPicker` route that `FileType.media` selects. So iOS asks first:
/// album or files.
///
/// Returns an empty list when the user backs out at either step.
Future<List<PlatformFile>> pickUploadFiles(
  BuildContext context,
  WidgetRef ref,
) async {
  var type = FileType.any;
  if (defaultTargetPlatform == TargetPlatform.iOS) {
    final chosen = await _showSourceSheet(context, ref);
    if (chosen == null) return const [];
    type = chosen;
  }
  return FilePickerPlatform.instance.pickFiles(type: type);
}

Future<FileType?> _showSourceSheet(BuildContext context, WidgetRef ref) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
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
          ListTile(
            dense: true,
            leading:
                Icon(Icons.photo_library_outlined, size: 18, color: t.n600),
            title: Text(
              i18n.t('resources:upload.fromPhotos'),
              style: TextStyle(fontSize: FontSizes.base, color: t.ink),
            ),
            onTap: () => Navigator.pop(sheetContext, FileType.media),
          ),
          ListTile(
            dense: true,
            leading: Icon(Icons.folder_open_outlined, size: 18, color: t.n600),
            title: Text(
              i18n.t('resources:upload.fromFiles'),
              style: TextStyle(fontSize: FontSizes.base, color: t.ink),
            ),
            onTap: () => Navigator.pop(sheetContext, FileType.any),
          ),
        ],
      ),
    ),
  );
}
