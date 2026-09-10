import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import 'admin_layout.dart';

/// Audit reasons and exact-target confirmation stay visible above the keyboard.
Future<bool> confirmAdminAction(
  BuildContext context, {
  required String title,
  required String body,
  required String confirm,
  required Future<void> Function(String note, CancelToken cancel) run,
  bool requireReason = false,
  bool showNote = false,
  bool retryOnError = true,
  String? target,
}) async =>
    await showModalBottomSheet<bool>(
      context: context,
      useSafeArea: true,
      isScrollControlled: true,
      isDismissible: false,
      enableDrag: false,
      backgroundColor: context.tokens.card,
      builder: (_) => _AdminConfirm(
        title: title,
        body: body,
        confirm: confirm,
        run: run,
        requireReason: requireReason,
        showNote: showNote,
        target: target,
        retryOnError: retryOnError,
      ),
    ) ??
    false;

class _AdminConfirm extends ConsumerStatefulWidget {
  const _AdminConfirm({
    required this.title,
    required this.body,
    required this.confirm,
    required this.run,
    required this.requireReason,
    required this.showNote,
    required this.retryOnError,
    this.target,
  });
  final String title, body, confirm;
  final bool requireReason, showNote, retryOnError;
  final String? target;
  final Future<void> Function(String, CancelToken) run;
  @override
  ConsumerState<_AdminConfirm> createState() => _AdminConfirmState();
}

class _AdminConfirmState extends ConsumerState<_AdminConfirm> {
  final _note = TextEditingController();
  final _target = TextEditingController();
  final _cancel = CancelToken();
  bool _busy = false;
  Object? _error;
  @override
  void dispose() {
    _cancel.cancel('Confirmation disposed');
    _note.dispose();
    _target.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_valid || _busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await widget.run(_note.text.trim(), _cancel);
      if (mounted && !_cancel.isCancelled) Navigator.pop(context, true);
    } catch (error) {
      if (mounted && !_cancel.isCancelled) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  bool get _valid =>
      (!widget.requireReason || _note.text.trim().isNotEmpty) &&
      (widget.target == null || _target.text.trim() == widget.target);

  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    final unknown =
        _error is DioException && (_error as DioException).response == null;
    return PopScope(
      canPop: !_busy,
      child: AdminSheet(
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              widget.title,
              style: TextStyle(
                color: context.tokens.ink,
                fontSize: FontSizes.xl2,
                fontWeight: FontWeight.w500,
              ),
            ),
            const SizedBox(height: 12),
            Text(
              widget.body,
              style: TextStyle(color: context.tokens.n700, height: 1.5),
            ),
            if (widget.requireReason || widget.showNote) ...[
              const SizedBox(height: 16),
              TextField(
                key: const ValueKey('admin-action-reason'),
                controller: _note,
                enabled: !_busy,
                maxLength: 1000,
                minLines: 2,
                maxLines: 4,
                onChanged: (_) => setState(() {}),
                decoration: InputDecoration(
                  labelText: i.t(
                    widget.requireReason
                        ? 'admin-skills:dialog.reason'
                        : 'admin-skills:dialog.note',
                  ),
                ),
              ),
            ],
            if (widget.target != null) ...[
              const SizedBox(height: 12),
              SelectableText(
                i.t(
                  'admin:mobile.confirmTarget',
                  vars: {'target': widget.target},
                ),
              ),
              TextField(
                key: const ValueKey('admin-action-target'),
                controller: _target,
                enabled: !_busy,
                autocorrect: false,
                enableSuggestions: false,
                smartQuotesType: SmartQuotesType.disabled,
                smartDashesType: SmartDashesType.disabled,
                onChanged: (_) => setState(() {}),
                decoration: InputDecoration(
                  labelText: i.t('admin:mobile.fieldTarget'),
                ),
              ),
            ],
            if (_error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 12),
                child: Text(
                  unknown
                      ? i.t('admin:mobile.unknownResult')
                      : errorText(i, _error!),
                  style: TextStyle(color: context.tokens.danger),
                ),
              ),
          ],
        ),
        footer: AdminActionBar(
          secondary: OutlinedButton(
            onPressed: _busy ? null : () => Navigator.pop(context, false),
            child: Text(i.t('common:action.cancel')),
          ),
          primary: FilledButton(
            key: const ValueKey('confirm-admin-action'),
            onPressed:
                _busy ||
                    !_valid ||
                    unknown ||
                    (_error != null && !widget.retryOnError)
                ? null
                : _submit,
            child: _busy
                ? const SizedBox.square(
                    dimension: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : Text(widget.confirm),
          ),
        ),
      ),
    );
  }
}
