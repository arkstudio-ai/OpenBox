import 'package:file_picker/file_picker.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../api/knowledge_api.dart';
import '../utils/knowledge_text.dart';
import 'knowledge_providers.dart';

/// What the knowledge page reads (web `ACCEPTED_FILES`).
const acceptedDocumentExtensions = [
  'pdf',
  'docx',
  'txt',
  'md',
  'markdown',
  'csv',
  'html',
  'htm',
];

/// The server refuses anything larger; say so before sending it.
const maxDocumentBytes = 10 * 1024 * 1024;

/// A file picked for upload: its name and size, its bytes on demand.
class PickedDocument {
  const PickedDocument({
    required this.name,
    required this.size,
    required this.read,
  });

  final String name;
  final int size;
  final Future<List<int>> Function() read;
}

typedef DocumentPicker = Future<List<PickedDocument>> Function();

/// The system document picker, limited to the formats the knowledge page
/// reads. Photos are not offered: an image is not a document it can organize.
final documentPickerProvider = Provider<DocumentPicker>(
  (ref) => () async {
    try {
      final files = await FilePickerPlatform.instance.pickFiles(
        type: FileType.custom,
        allowedExtensions: acceptedDocumentExtensions,
      );
      return [
        for (final file in files)
          PickedDocument(
            name: file.name,
            size: await file.length(),
            read: file.readAsBytes,
          ),
      ];
    } on PlatformException {
      // A refused permission or a picker that cannot open reads as nothing
      // picked; there is nothing useful to add to the system's own message.
      return const [];
    }
  },
);

/// Sends files one by one to the scope in view and reports each (web
/// `useFileUpload`). Oversized or empty files are stopped here. The batch is
/// pinned to the account and workspace it started in.
Future<void> uploadDocuments(
  WidgetRef ref, {
  required List<PickedDocument> files,
  required String projectId,
  required void Function(bool pending) onPending,
  required void Function(String error) onError,
  required bool Function() mounted,
}) async {
  final api = ref.read(knowledgeApiProvider);
  final toast = ref.read(toastProvider.notifier);
  final userId = ref.read(authSessionProvider).userId;
  final workspaceId = ref.read(workspaceScopeProvider).currentId;
  final i18n = ref.read(i18nProvider);
  for (final file in files) {
    if (!mounted()) return;
    if (file.size <= 0 || file.size > maxDocumentBytes) {
      onError(documentProblem(i18n, 'document_size_limit'));
      continue;
    }
    onPending(true);
    try {
      final doc = await api.upload(
        filename: file.name,
        bytes: await file.read(),
        projectId: projectId,
        userId: userId,
        workspaceId: workspaceId,
      );
      if (!mounted()) return;
      if (doc.created) {
        toast.success(
          i18n.t('knowledge:file.accepted', vars: {'name': doc.filename}),
        );
      } else {
        toast.info(
          i18n.t('knowledge:file.alreadyExists', vars: {'name': doc.filename}),
        );
      }
      refreshKnowledge(ref);
    } catch (error) {
      if (!mounted()) return;
      final code = apiErrorOf(error)?.code ?? '';
      onError(
        code.startsWith('DOCUMENT_')
            ? documentProblem(i18n, code.toLowerCase())
            : errorText(i18n, error),
      );
    } finally {
      if (mounted()) onPending(false);
    }
  }
}
