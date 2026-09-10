import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/archive_upload_queue.dart';
import '../api/admin_api.dart';
import '../widgets/admin_widgets.dart';

class AdminStoreUploadPage extends ConsumerStatefulWidget {
  const AdminStoreUploadPage({super.key});
  @override
  ConsumerState<AdminStoreUploadPage> createState() => _UploadState();
}

class _UploadState extends ConsumerState<AdminStoreUploadPage> {
  bool _busy = false;
  Future<List<ArchiveSelection>> _pick() async {
    final files = await FilePickerPlatform.instance.pickFiles(
      type: FileType.custom,
      allowedExtensions: ['zip'],
    );
    return [
      for (final file in files)
        ArchiveSelection(
          name: file.name,
          size: await file.length(),
          modified: await file.xFile.lastModified(),
          openRead: file.readAsByteStream,
        ),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    return PopScope(
      canPop: !_busy,
      child: Scaffold(
        backgroundColor: context.tokens.bg,
        appBar: AppBar(title: Text(i.t('admin-skills:manage.upload'))),
        body: AdminList(
          children: [
            AdminSectionHeading(
              i.t('admin:mobile.uploadZip'),
              subtitle: i.t('admin-skills:manage.uploadHint'),
            ),
            ProviderScope(
              overrides: [archivePickerProvider.overrideWithValue(_pick)],
              child: ArchiveUploadQueue(
                zipOnly: true,
                allowCustomName: false,
                onBusyChanged: (busy) => setState(() => _busy = busy),
                upload: (file, _, cancel) async {
                  await ref
                      .read(adminApiProvider)
                      .uploadSkill(
                        filename: file.name,
                        length: file.size,
                        openRead: file.openRead,
                        cancel: cancel,
                      );
                  if (mounted && !cancel.isCancelled) {
                    ref.read(adminSkillsChangedProvider)();
                  }
                },
              ),
            ),
          ],
        ),
        bottomNavigationBar: AdminActionBar(
          primary: OutlinedButton(
            onPressed: _busy ? null : () => Navigator.maybePop(context),
            child: Text(i.t('admin-skills:manage.close')),
          ),
        ),
      ),
    );
  }
}
