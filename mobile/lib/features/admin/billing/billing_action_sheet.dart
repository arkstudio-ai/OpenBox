import 'dart:convert';
import 'dart:math';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/labeled_checkbox.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../widgets/admin_layout.dart';

Future<bool> showAdminBillingAction(
  BuildContext context, {
  required AdminRecord detail,
  required String kind,
  AdminRecord? term,
}) async =>
    await showModalBottomSheet<bool>(
      context: context,
      useSafeArea: true,
      isScrollControlled: true,
      isDismissible: false,
      enableDrag: false,
      backgroundColor: context.tokens.card,
      builder: (_) =>
          AdminBillingActionSheet(detail: detail, kind: kind, term: term),
    ) ??
    false;

class AdminBillingActionSheet extends ConsumerStatefulWidget {
  const AdminBillingActionSheet({
    super.key,
    required this.detail,
    required this.kind,
    this.term,
  });
  final AdminRecord detail;
  final String kind;
  final AdminRecord? term;
  @override
  ConsumerState<AdminBillingActionSheet> createState() => _BillingActionState();
}

class _BillingActionState extends ConsumerState<AdminBillingActionSheet> {
  late final AdminApi _api;
  final _credits = TextEditingController();
  final _reason = TextEditingController();
  final _cancel = CancelToken();
  late String _kind, _storageKey;
  String _plan = 'pro', _cycle = 'monthly';
  AdminRecord? _term;
  DateTime? _end;
  Map<String, dynamic>? _pending;
  bool _confirmed = false, _busy = false;
  Object? _error;

  @override
  void initState() {
    super.initState();
    final api = ref.read(adminApiProvider);
    _api = api;
    _storageKey =
        'admin-billing-pending:${api.scope.userId}:${widget.detail.record('workspace').string('id')}';
    _kind = widget.kind;
    _term = widget.term;
    _plan = _term?.string('plan_id') ?? 'pro';
    _end = DateTime.tryParse(_term?.string('ends_at') ?? '')?.toLocal();
    try {
      final raw = ref.read(prefsProvider).getString(_storageKey);
      if (raw != null) {
        final value = jsonDecode(raw) as Map<String, dynamic>;
        final body = Map<String, dynamic>.from(value['body'] as Map);
        if ({'credits', 'grant', 'change', 'cancel'}.contains(value['kind']) &&
            body['request_key'] is String) {
          _pending = value;
          _kind = value['kind'] as String;
          _credits.text = body['credits'] as String? ?? '';
          _reason.text = body['reason'] as String? ?? '';
          _plan = body['plan_id'] as String? ?? 'pro';
          _cycle = body['cycle'] as String? ?? 'monthly';
          _end = DateTime.tryParse(body['ends_at'] as String? ?? '')?.toLocal();
          _term = widget.detail
              .records('history')
              .where((r) => r.string('id') == value['subscription_id'])
              .firstOrNull;
          _confirmed = true;
        }
      }
    } catch (_) {
      /* Invalid local draft must not issue a request. */
    }
  }

  @override
  void dispose() {
    _cancel.cancel('Billing sheet disposed');
    _credits.dispose();
    _reason.dispose();
    super.dispose();
  }

  bool get _fixed => _busy || _pending != null;
  bool get _valid {
    final value = double.tryParse(_credits.text.trim());
    return _reason.text.trim().isNotEmpty &&
        _reason.text.trim().length <= 1000 &&
        (_kind != 'change' || _end != null) &&
        (_kind != 'credits' ||
            (RegExp(r'^\d+(\.\d{1,6})?$').hasMatch(_credits.text.trim()) &&
                value != null &&
                value > 0 &&
                value <= 1000000));
  }

  void _changed() => setState(() => _confirmed = false);

  Future<void> _date() async {
    final now = DateTime.now();
    final day = await showDatePicker(
      context: context,
      initialDate: _end ?? now.add(const Duration(days: 30)),
      firstDate: now,
      lastDate: DateTime(now.year + 10, now.month, now.day),
    );
    if (day == null || !mounted) return;
    final time = await showTimePicker(
      context: context,
      initialTime: TimeOfDay.fromDateTime(_end ?? now),
    );
    if (time == null || !mounted) return;
    setState(() {
      _end = DateTime(day.year, day.month, day.day, time.hour, time.minute);
      _confirmed = false;
    });
  }

  Future<void> _submit() async {
    if (_busy || !_valid || !_confirmed) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final api = _api;
    final prefs = ref.read(prefsProvider);
    final random = Random.secure();
    final write =
        _pending ??
        <String, dynamic>{
          'kind': _kind,
          'subscription_id': _term?.string('id'),
          'body': <String, dynamic>{
            'request_key': List.generate(
              24,
              (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
            ).join(),
            'reason': _reason.text.trim(),
            if (_kind == 'credits') 'credits': _credits.text.trim(),
            if (_kind == 'grant' || _kind == 'change') 'plan_id': _plan,
            if (_kind == 'grant') 'cycle': _cycle,
            if (_kind == 'change') 'ends_at': _end!.toUtc().toIso8601String(),
            if (_kind == 'change' || _kind == 'cancel')
              'expected_revision': _term?.string('revision'),
          },
        };
    setState(() => _pending = write);
    try {
      await prefs.setString(_storageKey, jsonEncode(write));
      await api.manageBilling(
        widget.detail.record('workspace').string('id'),
        write['kind'] as String,
        Map<String, dynamic>.from(write['body'] as Map),
        _cancel,
        subscriptionId: write['subscription_id'] as String?,
      );
      await prefs.remove(_storageKey);
      if (mounted && !_cancel.isCancelled) Navigator.pop(context, true);
    } catch (error) {
      final failure = apiErrorOf(error);
      if (failure != null && failure.status >= 400 && failure.status < 500) {
        await prefs.remove(_storageKey);
        if (mounted) setState(() => _pending = null);
      }
      if (mounted && !_cancel.isCancelled) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    String t(String key, [Map<String, dynamic>? vars]) =>
        i.t('admin-billing:$key', vars: vars ?? {});
    final target = {
      'user': widget.detail.record('owner').string('username'),
      'workspace': widget.detail.record('workspace').string('name'),
    };
    final failure = _error == null ? null : apiErrorOf(_error!);
    final code = failure == null || failure.status == 0 || failure.status >= 500
        ? 'UNKNOWN_RESULT'
        : failure.code;
    final mapped = t('actionErrors.$code');
    return PopScope(
      canPop: !_busy,
      child: AdminSheet(
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              t('actions.$_kind'),
              style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w500),
            ),
            const SizedBox(height: 12),
            Text(
              '${target['user']} · ${target['workspace']}',
              style: const TextStyle(fontWeight: FontWeight.w500),
            ),
            Text(
              t('actions.currentBalance', {
                'balance': widget.detail.string('balance'),
              }),
            ),
            const SizedBox(height: 12),
            Text(
              t('actions.${_kind}Hint'),
              style: TextStyle(
                fontSize: 12,
                height: 1.5,
                color: context.tokens.n600,
              ),
            ),
            if (_kind == 'cancel' && _term != null) ...[
              const SizedBox(height: 12),
              Text(
                "${t('plans.${_term!.string('plan_id')}')} · "
                "${DateTime.parse(_term!.string('starts_at')).toLocal().toString().substring(0, 16)} → "
                "${DateTime.parse(_term!.string('ends_at')).toLocal().toString().substring(0, 16)}",
              ),
            ],
            const SizedBox(height: 16),
            if (_kind == 'credits')
              TextField(
                key: const ValueKey('billing-credits'),
                controller: _credits,
                enabled: !_fixed,
                keyboardType: const TextInputType.numberWithOptions(
                  decimal: true,
                ),
                onChanged: (_) => _changed(),
                decoration: InputDecoration(
                  labelText: t('actions.creditsAmount'),
                  helperText: t('actions.creditsLimit'),
                  helperMaxLines: 3,
                ),
              ),
            if (_kind == 'grant' || _kind == 'change')
              DropdownButtonFormField<String>(
                initialValue: _plan,
                decoration: InputDecoration(labelText: t('filters.plan')),
                items: [
                  for (final id in ['pro', 'max'])
                    DropdownMenuItem(value: id, child: Text(t('plans.$id'))),
                ],
                onChanged: _fixed
                    ? null
                    : (v) {
                        _plan = v!;
                        _changed();
                      },
              ),
            if (_kind == 'grant') ...[
              const SizedBox(height: 16),
              DropdownButtonFormField<String>(
                initialValue: _cycle,
                decoration: InputDecoration(labelText: t('actions.duration')),
                items: [
                  DropdownMenuItem(
                    value: 'monthly',
                    child: Text(t('actions.oneMonth')),
                  ),
                  DropdownMenuItem(
                    value: 'yearly',
                    child: Text(t('actions.oneYear')),
                  ),
                ],
                onChanged: _fixed
                    ? null
                    : (v) {
                        _cycle = v!;
                        _changed();
                      },
              ),
            ],
            if (_kind == 'change') ...[
              const SizedBox(height: 16),
              OutlinedButton(
                onPressed: _fixed ? null : _date,
                child: Text(
                  '${t('actions.endsAt')}\n${_end == null ? t('actions.datePrompt') : _end.toString().substring(0, 16)}',
                ),
              ),
            ],
            const SizedBox(height: 16),
            TextField(
              key: const ValueKey('billing-reason'),
              controller: _reason,
              enabled: !_fixed,
              maxLength: 1000,
              minLines: 2,
              maxLines: 4,
              onChanged: (_) => _changed(),
              decoration: InputDecoration(labelText: t('actions.reason')),
            ),
            IgnorePointer(
              ignoring: _fixed || !_valid,
              child: Opacity(
                opacity: !_valid ? .5 : 1,
                child: LabeledCheckbox(
                  value: _confirmed,
                  label: t('actions.confirmTarget', target),
                  onChanged: (v) => setState(() => _confirmed = v),
                ),
              ),
            ),
            if (_error != null)
              Text(
                mapped == 'admin-billing:actionErrors.$code'
                    ? t('actionErrors.default')
                    : mapped,
                style: TextStyle(color: context.tokens.danger),
              ),
          ],
        ),
        footer: AdminActionBar(
          secondary: OutlinedButton(
            onPressed: _busy ? null : () => Navigator.pop(context, false),
            child: Text(t('actions.close')),
          ),
          primary: FilledButton(
            key: const ValueKey('submit-billing-action'),
            onPressed: _busy || !_valid || !_confirmed ? null : _submit,
            child: Text(
              t(
                _busy
                    ? 'actions.saving'
                    : _pending != null
                    ? 'actions.retry'
                    : 'actions.$_kind',
              ),
            ),
          ),
        ),
      ),
    );
  }
}
