import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/api/providers.dart';
import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../../../shared/utils/error_text.dart';
import '../../../../shared/widgets/toast.dart';
import '../../api/assets_api.dart';
import '../../state/question_draft.dart';
import '../composer/resource_slot.dart';

/// Uses durable asset IDs for both library picks and phone uploads. The draft
/// is shared with voice; adding a resource never submits the user's answer.
class QuestionResources extends ConsumerStatefulWidget {
  const QuestionResources({
    super.key,
    required this.request,
    required this.index,
  });
  final QuestionRequest request;
  final int index;

  @override
  ConsumerState<QuestionResources> createState() => _QuestionResourcesState();
}

class _QuestionResourcesState extends ConsumerState<QuestionResources> {
  bool _browsing = false;
  String _query = '';

  int get _count => widget.request.questions.length;

  void _pick(String id) {
    final store = ref.read(questionDraftProvider.notifier);
    final draft = store.of(widget.request.id, _count);
    if (draft.submitting || draft.transferring) return;
    final selected = draft.attachmentIds[widget.index];
    if (selected.contains(id)) return;
    if (draft.attachmentIds.fold<int>(0, (sum, row) => sum + row.length) >=
        32) {
      ref
          .read(toastProvider.notifier)
          .error(ref.read(i18nProvider).t('chat:question.resourceLimit'));
      return;
    }
    store.setAttachments(widget.request.id, _count, widget.index, [
      ...selected,
      id,
    ]);
  }

  Future<void> _upload(ComposerResourceSlot slot) async {
    final store = ref.read(questionDraftProvider.notifier);
    final id = widget.request.id, index = widget.index, count = _count;
    final draft = store.of(id, count);
    if (draft.submitting || draft.transferring) return;
    final auth = ref.read(authSessionProvider),
        workspace = ref.read(workspaceScopeProvider);
    final userId = auth.userId, workspaceId = workspace.currentId;
    final toast = ref.read(toastProvider.notifier),
        i18n = ref.read(i18nProvider);
    bool current() =>
        auth.userId == userId &&
        workspace.currentId == workspaceId &&
        store.contains(id);
    store.setTransferring(id, count, true);
    try {
      final uploaded = await slot.pickAndUpload(context, projectId: null);
      if (!current()) return;
      final live = store.of(id, count);
      final selected = [...live.attachmentIds[index]];
      final otherCount = live.attachmentIds.indexed
          .where((row) => row.$1 != index)
          .fold<int>(0, (sum, row) => sum + row.$2.length);
      for (final resource in uploaded) {
        if (selected.contains(resource.id)) continue;
        if (selected.length + otherCount >= 32) {
          toast.error(i18n.t('chat:question.resourceLimit'));
          break;
        }
        selected.add(resource.id);
      }
      store.setAttachments(id, count, index, selected);
    } catch (error) {
      if (current()) toast.error(errorText(i18n, error));
    } finally {
      if (current()) store.setTransferring(id, count, false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final draft =
        ref.watch(questionDraftProvider)[widget.request.id] ??
        QuestionDraft.empty(_count);
    final selected = draft.attachmentIds[widget.index];
    final disabled = draft.submitting || draft.transferring;
    final slot = ComposerResources.maybeOf(context);
    return Padding(
      padding: const EdgeInsets.only(top: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            i18n.t('chat:question.resourcesHint'),
            style: TextStyle(color: t.n600),
          ),
          if (slot != null)
            Wrap(
              spacing: 8,
              children: [
                OutlinedButton.icon(
                  onPressed: disabled
                      ? null
                      : () => setState(() => _browsing = !_browsing),
                  icon: const Icon(Icons.photo_library_outlined, size: 18),
                  label: Text(i18n.t('chat:composer.resourceCenter')),
                ),
                OutlinedButton.icon(
                  onPressed: disabled ? null : () => _upload(slot),
                  icon: const Icon(Icons.upload_outlined, size: 18),
                  label: Text(i18n.t('chat:composer.uploadFile')),
                ),
              ],
            ),
          if (_browsing && slot != null) ...[
            TextField(
              enabled: !disabled,
              decoration: InputDecoration(
                hintText: i18n.t('chat:question.searchResources'),
              ),
              onChanged: (value) => setState(() => _query = value),
            ),
            IgnorePointer(
              ignoring: disabled,
              child: slot.mentionSection(
                context,
                query: _query,
                projectId: null,
                onPick: (resource) => _pick(resource.id),
              ),
            ),
          ],
          for (final id in selected)
            _SelectedResource(
              id: id,
              disabled: disabled,
              onRemove: () => ref
                  .read(questionDraftProvider.notifier)
                  .setAttachments(
                    widget.request.id,
                    _count,
                    widget.index,
                    selected.where((value) => value != id).toList(),
                  ),
            ),
          if (draft.transferring) ...[
            const LinearProgressIndicator(),
            Text(i18n.t('chat:question.resourcesUploading')),
          ],
        ],
      ),
    );
  }
}

class _SelectedResource extends ConsumerWidget {
  const _SelectedResource({
    required this.id,
    required this.disabled,
    required this.onRemove,
  });
  final String id;
  final bool disabled;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final asset = ref.watch(assetUrlProvider(id));
    final info = asset.valueOrNull;
    final name =
        info?.name ??
        i18n.t(
          asset.hasError
              ? 'chat:question.resourceUnavailable'
              : 'chat:question.resourceLoading',
        );
    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: info != null && (info.mime?.startsWith('image/') ?? false)
          ? Image.network(
              info.url,
              width: 44,
              height: 44,
              fit: BoxFit.cover,
              errorBuilder: (_, _, _) =>
                  const Icon(Icons.broken_image_outlined),
            )
          : const Icon(Icons.insert_drive_file_outlined),
      title: Text(name, maxLines: 2, overflow: TextOverflow.ellipsis),
      trailing: IconButton(
        tooltip: i18n.t('chat:question.removeResource', vars: {'name': name}),
        onPressed: disabled ? null : onRemove,
        icon: const Icon(Icons.close),
      ),
    );
  }
}
