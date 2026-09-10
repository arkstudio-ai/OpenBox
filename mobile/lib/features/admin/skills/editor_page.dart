import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';
import '../widgets/admin_widgets.dart';

class AdminSkillEditorPage extends ConsumerStatefulWidget {
  const AdminSkillEditorPage({super.key, this.catalogId});
  final String? catalogId;
  @override
  ConsumerState<AdminSkillEditorPage> createState() => _EditorPageState();
}

class _EditorPageState
    extends AdminLoadState<AdminRecord, AdminSkillEditorPage> {
  @override
  bool get loadOnMount => widget.catalogId != null;
  @override
  Future<AdminRecord> fetch(CancelToken cancel) =>
      api.skillDetail(widget.catalogId!, cancel);
  @override
  Widget build(BuildContext context) => widget.catalogId == null
      ? const _EditorForm()
      : data != null
      ? _EditorForm(entry: data)
      : Scaffold(
          appBar: AppBar(title: Text(i18n.t('admin-skills:manage.edit'))),
          body: loadable((_) => const SizedBox.shrink()),
        );
}

class _EditorForm extends ConsumerStatefulWidget {
  const _EditorForm({this.entry});
  final AdminRecord? entry;
  @override
  ConsumerState<_EditorForm> createState() => _EditorFormState();
}

class _EditorFormState extends ConsumerState<_EditorForm> {
  // Freeze both the original content and its revision for this draft.
  late final _entry = widget.entry;
  late String _kind = _entry?.string('kind', 'skill') ?? 'skill';
  late final _name = TextEditingController(text: _entry?.string('name') ?? '');
  late final _title = TextEditingController(
    text: _entry?.string('title') ?? '',
  );
  late final _description = TextEditingController(
    text: _entry?.string('description') ?? '',
  );
  late final _icon = TextEditingController(text: _entry?.string('icon') ?? '');
  late final _content = TextEditingController(
    text:
        _entry?.string('content') ??
        '---\nname: my-skill\ndescription: Describe what this skill does.\n---\n\n# My skill\n',
  );
  late final _config = TextEditingController(
    text: const JsonEncoder.withIndent('  ').convert(
      _entry?.data['config'] ??
          {'type': 'stdio', 'command': '', 'args': <String>[]},
    ),
  );
  final _form = GlobalKey<FormState>();
  final _cancel = CancelToken();
  bool _busy = false;
  bool _unknownResult = false;
  String? _error;
  @override
  void dispose() {
    _cancel.cancel('Editor disposed');
    for (final c in [_name, _title, _description, _icon, _content, _config]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _save() async {
    if (_busy || _unknownResult || !_form.currentState!.validate()) return;
    final i = ref.read(i18nProvider);
    final fields = <String, dynamic>{
      'title': _title.text.trim(),
      'description': _description.text.trim(),
      'icon': _icon.text.trim(),
    };
    if (_kind == 'skill') {
      if (_entry == null || _content.text != _entry.string('content')) {
        fields['content'] = _content.text;
      }
    } else {
      try {
        final config = jsonDecode(_config.text);
        if (config is! Map<String, dynamic>) throw const FormatException();
        fields['config'] = config;
      } catch (_) {
        setState(() => _error = i.t('admin-skills:manage.invalidJson'));
        return;
      }
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref
          .read(adminApiProvider)
          .saveSkill(
            id: _entry?.string('catalog_id'),
            name: _name.text.trim(),
            kind: _kind,
            revision: _entry?.integer('revision') ?? 0,
            fields: fields,
            cancel: _cancel,
          );
      if (mounted && !_cancel.isCancelled) Navigator.pop(context, true);
    } catch (error) {
      if (mounted && !_cancel.isCancelled) {
        setState(() {
          _unknownResult = error is DioException && error.response == null;
          _error = _unknownResult
              ? i.t('admin:mobile.unknownResult')
              : errorText(i, error);
        });
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    return PopScope(
      canPop: !_busy,
      child: Scaffold(
        backgroundColor: context.tokens.bg,
        appBar: AppBar(
          title: Text(
            i.t(
              _entry == null
                  ? 'admin-skills:manage.create'
                  : 'admin-skills:manage.edit',
            ),
          ),
          leading: BackButton(
            onPressed: _busy ? null : () => Navigator.maybePop(context),
          ),
        ),
        body: Form(
          key: _form,
          child: Column(
            children: [
              Expanded(
                child: AdminList(
                  children: [
                    AdminCard(
                      title: i.t('admin:mobile.editorIdentity'),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Text(
                            i.t(
                              _entry == null
                                  ? 'admin-skills:manage.createHint'
                                  : 'admin-skills:manage.editHint',
                            ),
                            style: TextStyle(
                              color: context.tokens.n600,
                              fontSize: FontSizes.sm,
                            ),
                          ),
                          const SizedBox(height: 18),
                          DropdownButtonFormField<String>(
                            initialValue: _kind,
                            isExpanded: true,
                            decoration: InputDecoration(
                              labelText: i.t('admin-skills:store.filter.kind'),
                            ),
                            items: [
                              for (final kind in ['skill', 'mcp'])
                                DropdownMenuItem(
                                  value: kind,
                                  child: Text(
                                    i.t('admin-skills:store.kind.$kind'),
                                  ),
                                ),
                            ],
                            onChanged: _busy || _entry != null
                                ? null
                                : (kind) => setState(() => _kind = kind!),
                          ),
                          const SizedBox(height: 18),
                          TextFormField(
                            key: const ValueKey('admin-editor-name'),
                            controller: _name,
                            enabled: !_busy && _entry == null,
                            autocorrect: false,
                            smartQuotesType: SmartQuotesType.disabled,
                            smartDashesType: SmartDashesType.disabled,
                            maxLength: 64,
                            decoration: InputDecoration(
                              labelText: i.t('admin-skills:manage.name'),
                            ),
                            validator: (value) =>
                                _entry != null ||
                                    RegExp(
                                      r'^[a-z0-9]+(-[a-z0-9]+)*$',
                                    ).hasMatch(value?.trim() ?? '')
                                ? null
                                : i.t('admin:mobile.invalidName'),
                          ),
                        ],
                      ),
                    ),
                    AdminCard(
                      title: i.t('admin:mobile.editorPresentation'),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          TextFormField(
                            key: const ValueKey('admin-editor-title'),
                            controller: _title,
                            enabled: !_busy,
                            maxLength: 120,
                            decoration: InputDecoration(
                              labelText: i.t('admin-skills:manage.title'),
                            ),
                            validator: (value) =>
                                (value?.trim().isEmpty ?? true)
                                ? i.t('admin-skills:manage.title')
                                : null,
                          ),
                          const SizedBox(height: 12),
                          TextFormField(
                            controller: _description,
                            enabled: !_busy,
                            maxLength: 4000,
                            minLines: 2,
                            maxLines: 4,
                            decoration: InputDecoration(
                              labelText: i.t('admin-skills:manage.description'),
                            ),
                          ),
                          const SizedBox(height: 12),
                          TextFormField(
                            key: const ValueKey('admin-editor-icon'),
                            controller: _icon,
                            enabled: !_busy,
                            maxLength: 2048,
                            onChanged: (_) => setState(() {}),
                            decoration: InputDecoration(
                              labelText: i.t('admin-skills:manage.icon'),
                              prefixIcon: Padding(
                                padding: const EdgeInsets.all(8),
                                child: AdminIcon(_icon.text),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    AdminCard(
                      title: i.t(
                        _kind == 'skill'
                            ? 'admin:mobile.editorContent'
                            : 'admin-skills:manage.config',
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          TextFormField(
                            key: const ValueKey('admin-editor-content'),
                            controller: _kind == 'skill' ? _content : _config,
                            enabled: !_busy,
                            minLines: 9,
                            maxLines: 18,
                            maxLength: 65536,
                            autocorrect: false,
                            enableSuggestions: false,
                            smartQuotesType: SmartQuotesType.disabled,
                            smartDashesType: SmartDashesType.disabled,
                            style: const TextStyle(
                              fontFamily: 'monospace',
                              fontSize: FontSizes.sm,
                            ),
                            decoration: InputDecoration(
                              labelText: i.t(
                                _kind == 'skill'
                                    ? 'admin-skills:manage.content'
                                    : 'admin-skills:manage.config',
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxHeight: 96),
                    child: SingleChildScrollView(
                      child: Text(
                        _error!,
                        style: TextStyle(color: context.tokens.danger),
                      ),
                    ),
                  ),
                ),
              AdminActionBar(
                primary: FilledButton(
                  key: const ValueKey('save-admin-entry'),
                  onPressed: _busy || _unknownResult ? null : _save,
                  child: Text(
                    i.t(
                      _busy
                          ? 'admin-skills:list.loading'
                          : 'admin-skills:manage.save',
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
