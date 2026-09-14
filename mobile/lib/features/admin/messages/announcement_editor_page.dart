import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/utils/format.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../widgets/admin_widgets.dart';

final _slug = RegExp(r'^[a-z0-9][a-z0-9-]{1,63}$');

/// Draft editor for one announcement (web `AnnouncementForm`). Validation
/// mirrors the server; pops `true` after a successful save.
class AdminAnnouncementEditorPage extends ConsumerStatefulWidget {
  const AdminAnnouncementEditorPage({super.key, this.source});
  final AdminRecord? source;
  @override
  ConsumerState<AdminAnnouncementEditorPage> createState() => _EditorState();
}

class _EditorState extends ConsumerState<AdminAnnouncementEditorPage> {
  AdminRecord? get _source => widget.source;
  late final _title = TextEditingController(text: _source?.string('title'));
  late final _body = TextEditingController(text: _source?.string('body'));
  late String _linkKind =
      _source?.record('link').string('kind', 'none') ?? 'none';
  late final _linkValue = TextEditingController(
    text: switch (_linkKind) {
      'topic' => _source?.record('link').string('slug') ?? '',
      'url' => _source?.record('link').string('url') ?? '',
      _ => '',
    },
  );
  late String _audienceKind =
      _source?.record('audience').string('kind', 'all') ?? 'all';
  late String _role =
      _source?.record('audience').string('role', 'user') ?? 'user';
  late final _workspaceId = TextEditingController(
    text: _source?.record('audience').string('id'),
  );
  late final _userIds = TextEditingController(
    text: _source?.record('audience').strings('ids').join('\n'),
  );
  late bool _push = _source?.flag('push') ?? false;
  late DateTime? _publishAt = DateTime.tryParse(
    _source?.string('publishAt') ?? '',
  )?.toLocal();
  late DateTime? _expiresAt = DateTime.tryParse(
    _source?.string('expiresAt') ?? '',
  )?.toLocal();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    for (final c in [_title, _body, _linkValue, _workspaceId, _userIds]) {
      c.dispose();
    }
    super.dispose();
  }

  Map<String, dynamic>? _payload() {
    final title = _title.text.trim();
    if (title.isEmpty) return null;
    Map<String, dynamic>? link;
    if (_linkKind == 'topic') {
      final slug = _linkValue.text.trim();
      if (!_slug.hasMatch(slug)) return null;
      link = {'kind': 'topic', 'slug': slug};
    } else if (_linkKind == 'url') {
      final url = _linkValue.text.trim();
      if (!url.startsWith('https://')) return null;
      link = {'kind': 'url', 'url': url};
    }
    final Map<String, dynamic> audience;
    switch (_audienceKind) {
      case 'role':
        audience = {'kind': 'role', 'role': _role};
      case 'workspace':
        final id = _workspaceId.text.trim();
        if (id.isEmpty) return null;
        audience = {'kind': 'workspace', 'id': id};
      case 'users':
        final ids = _userIds.text
            .split(RegExp(r'[\s,]+'))
            .map((v) => v.trim())
            .where((v) => v.isNotEmpty)
            .toSet()
            .toList();
        if (ids.isEmpty || ids.length > 5000) return null;
        audience = {'kind': 'users', 'ids': ids};
      default:
        audience = {'kind': 'all'};
    }
    final now = DateTime.now();
    if (_expiresAt != null && !_expiresAt!.isAfter(now)) return null;
    if (_expiresAt != null &&
        _publishAt != null &&
        !_expiresAt!.isAfter(_publishAt!)) {
      return null;
    }
    return {
      'title': title,
      'body': _body.text.trim(),
      'link': link,
      'audience': audience,
      'push': _push,
      'publishAt': _publishAt?.toUtc().toIso8601String(),
      'expiresAt': _expiresAt?.toUtc().toIso8601String(),
    };
  }

  Future<void> _save() async {
    if (_busy) return;
    final i = ref.read(i18nProvider);
    final payload = _payload();
    if (payload == null) {
      setState(() => _error = i.t('admin-messages:form.invalid'));
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final api = ref.read(adminApiProvider);
      if (_source == null) {
        await api.createAnnouncement(payload);
      } else {
        await api.updateAnnouncement(_source!.string('id'), payload);
      }
      if (mounted) Navigator.pop(context, true);
    } catch (error) {
      if (mounted) setState(() => _error = errorText(i, error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _pick(DateTime? current, ValueChanged<DateTime?> apply) async {
    final base = current ?? DateTime.now().add(const Duration(hours: 1));
    final date = await showDatePicker(
      context: context,
      initialDate: base,
      firstDate: DateTime.now().subtract(const Duration(days: 1)),
      lastDate: DateTime.now().add(const Duration(days: 365)),
    );
    if (date == null || !mounted) return;
    final time = await showTimePicker(
      context: context,
      initialTime: TimeOfDay.fromDateTime(base),
    );
    if (time == null || !mounted) return;
    apply(DateTime(date.year, date.month, date.day, time.hour, time.minute));
  }

  Widget _dateRow(
    String label,
    String hint,
    DateTime? value,
    ValueChanged<DateTime?> apply,
  ) {
    final i = ref.watch(i18nProvider);
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: InputDecorator(
        decoration: InputDecoration(labelText: label, helperText: hint),
        child: Row(
          children: [
            Expanded(
              child: Text(
                value == null ? '—' : formatDateTime(value, i.language),
                style: TextStyle(color: t.ink, fontSize: FontSizes.base),
              ),
            ),
            if (value != null)
              IconButton(
                visualDensity: VisualDensity.compact,
                onPressed: _busy ? null : () => setState(() => apply(null)),
                icon: const Icon(Icons.close, size: 18),
              ),
            IconButton(
              visualDensity: VisualDensity.compact,
              onPressed: _busy
                  ? null
                  : () =>
                        _pick(value, (picked) => setState(() => apply(picked))),
              icon: const Icon(Icons.event, size: 18),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    final t = context.tokens;
    InputDecoration deco(String label, [String? hint]) =>
        InputDecoration(labelText: label, helperText: hint);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Text(
          i.t(
            _source == null
                ? 'admin-messages:form.newTitle'
                : 'admin-messages:form.editTitle',
          ),
        ),
      ),
      body: ListView(
        keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
        children: [
          TextField(
            key: const ValueKey('announcement-title'),
            controller: _title,
            enabled: !_busy,
            maxLength: 120,
            decoration: deco(i.t('admin-messages:form.title')),
          ),
          TextField(
            key: const ValueKey('announcement-body'),
            controller: _body,
            enabled: !_busy,
            maxLength: 500,
            minLines: 2,
            maxLines: 4,
            decoration: deco(
              i.t('admin-messages:form.body'),
              i.t('admin-messages:form.bodyHint'),
            ),
          ),
          AdminSectionHeading(i.t('admin-messages:form.link')),
          SegmentedButton<String>(
            segments: [
              for (final kind in ['none', 'topic', 'url'])
                ButtonSegment(
                  value: kind,
                  label: Text(
                    i.t(
                      'admin-messages:form.link${kind[0].toUpperCase()}${kind.substring(1)}',
                    ),
                  ),
                ),
            ],
            selected: {_linkKind},
            onSelectionChanged: _busy
                ? null
                : (value) => setState(() => _linkKind = value.first),
          ),
          if (_linkKind != 'none')
            TextField(
              key: const ValueKey('announcement-link-value'),
              controller: _linkValue,
              enabled: !_busy,
              decoration: deco(
                i.t(
                  _linkKind == 'topic'
                      ? 'admin-messages:form.topicSlug'
                      : 'admin-messages:form.url',
                ),
              ),
            ),
          AdminSectionHeading(i.t('admin-messages:form.audience')),
          DropdownButtonFormField<String>(
            key: const ValueKey('announcement-audience'),
            initialValue: _audienceKind,
            items: [
              for (final kind in ['all', 'role', 'workspace', 'users'])
                DropdownMenuItem(
                  value: kind,
                  child: Text(
                    i.t(
                      'admin-messages:form.audience${kind[0].toUpperCase()}${kind.substring(1)}',
                    ),
                  ),
                ),
            ],
            onChanged: _busy
                ? null
                : (value) => setState(() => _audienceKind = value ?? 'all'),
          ),
          if (_audienceKind == 'role')
            DropdownButtonFormField<String>(
              initialValue: _role,
              decoration: deco(i.t('admin-messages:form.role')),
              items: [
                DropdownMenuItem(
                  value: 'user',
                  child: Text(i.t('admin-messages:form.roleUser')),
                ),
                DropdownMenuItem(
                  value: 'admin',
                  child: Text(i.t('admin-messages:form.roleAdmin')),
                ),
              ],
              onChanged: _busy
                  ? null
                  : (value) => setState(() => _role = value ?? 'user'),
            ),
          if (_audienceKind == 'workspace')
            TextField(
              controller: _workspaceId,
              enabled: !_busy,
              decoration: deco(i.t('admin-messages:form.workspaceId')),
            ),
          if (_audienceKind == 'users')
            TextField(
              key: const ValueKey('announcement-user-ids'),
              controller: _userIds,
              enabled: !_busy,
              minLines: 3,
              maxLines: 8,
              decoration: deco(
                i.t('admin-messages:form.userIds'),
                i.t('admin-messages:form.userIdsHint'),
              ),
            ),
          SwitchListTile(
            key: const ValueKey('announcement-push'),
            contentPadding: EdgeInsets.zero,
            value: _push,
            onChanged: _busy ? null : (value) => setState(() => _push = value),
            title: Text(i.t('admin-messages:form.push')),
            subtitle: Text(
              i.t('admin-messages:form.pushHint'),
              style: TextStyle(color: t.n600, fontSize: FontSizes.xs),
            ),
          ),
          _dateRow(
            i.t('admin-messages:form.publishAt'),
            i.t('admin-messages:form.publishAtHint'),
            _publishAt,
            (value) => _publishAt = value,
          ),
          _dateRow(
            i.t('admin-messages:form.expiresAt'),
            i.t('admin-messages:form.expiresAtHint'),
            _expiresAt,
            (value) => _expiresAt = value,
          ),
        ],
      ),
      // The error sits with the action bar so it is visible without scrolling.
      bottomNavigationBar: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (_error != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
              child: Text(
                _error!,
                key: const ValueKey('announcement-error'),
                style: TextStyle(color: t.danger, fontSize: FontSizes.sm),
              ),
            ),
          AdminActionBar(
            secondary: OutlinedButton(
              onPressed: _busy ? null : () => Navigator.pop(context, false),
              child: Text(i.t('admin-messages:common.cancel')),
            ),
            primary: FilledButton(
              key: const ValueKey('announcement-save'),
              onPressed: _busy ? null : _save,
              child: Text(
                i.t(
                  _busy
                      ? 'admin-messages:common.saving'
                      : 'admin-messages:common.save',
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
