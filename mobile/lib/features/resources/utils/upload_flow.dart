import 'package:file_picker/file_picker.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/resource.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../api/resources_api.dart';
import 'resource_display.dart';

/// Pick files off the device and upload them into [projectId], returning what
/// landed. Shared by the resource centre's "+" and the composer's, so both
/// entries file uploads the same way.
///
/// A failed file is reported and skipped — the rest of the batch still lands,
/// which beats failing the whole pick.
Future<List<Resource>> pickAndUploadResources(
  WidgetRef ref, {
  required String? projectId,
}) async {
  final picked = await FilePickerPlatform.instance.pickFiles();
  if (picked.isEmpty) return const [];

  final api = ref.read(resourcesApiProvider);
  final landed = <Resource>[];
  for (final file in picked) {
    try {
      final bytes = await file.readAsBytes();
      landed.add(
        await api.upload(
          name: file.name,
          mime: mimeForName(file.name),
          bytes: bytes,
          projectId: projectId,
        ),
      );
    } catch (error) {
      ref.read(toastProvider.notifier).error(
            '${ref.read(i18nProvider).t('resources:upload.failed', vars: {
                  'name': file.name,
                })} — ${errorText(ref.read(i18nProvider), error)}',
          );
    }
  }
  if (landed.isNotEmpty) bumpResources(ref);
  return landed;
}
