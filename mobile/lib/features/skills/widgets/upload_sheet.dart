import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/section_tabs.dart';
import '../utils/parse_mcp_config.dart';
import 'archive_upload_queue.dart';
import 'mcp_config_form.dart';
import 'sheet_scaffold.dart';

/// What the add sheet came back with — exactly one of these is set.
class UploadRequest {
  const UploadRequest.skill(this.skill) : mcp = null;
  const UploadRequest.mcp(this.mcp) : skill = null;

  /// `{url, name, content}` for `/api/agent/skill/install`.
  final Map<String, String?>? skill;
  final List<ParsedMcpEntry>? mcp;
}

/// Adding something the store does not carry (web `UploadDialog`).
///
/// Four routes in, because that is how skills and servers actually arrive:
/// an archive someone exported, a SKILL.md pasted from an editor, a git repo,
/// and — for MCP — the JSON snippet every server's README hands out.
class UploadSheet extends ConsumerStatefulWidget {
  const UploadSheet({
    super.key,
    required this.busy,
    required this.error,
    required this.onSubmit,
    required this.onUploadArchive,
  });

  final bool busy;
  final String? error;
  final void Function(UploadRequest request) onSubmit;
  final ArchiveUploader onUploadArchive;

  @override
  ConsumerState<UploadSheet> createState() => _UploadSheetState();
}

class _UploadSheetState extends ConsumerState<UploadSheet> {
  static const _modes = ['archive', 'paste', 'git', 'mcp'];

  String _mode = 'archive';
  bool _queueBusy = false;
  final _name = TextEditingController();
  final _content = TextEditingController();
  final _url = TextEditingController();
  final _mcp = McpFormState();

  @override
  void dispose() {
    _name.dispose();
    _content.dispose();
    _url.dispose();
    _mcp.dispose();
    super.dispose();
  }

  bool get _canSubmit => switch (_mode) {
    'archive' => true,
    'paste' => _content.text.trim().isNotEmpty,
    'git' => _url.text.trim().isNotEmpty,
    _ => _mcp.canSubmit,
  };

  void _submit() {
    final i18n = ref.read(i18nProvider);
    switch (_mode) {
      case 'archive':
        Navigator.of(context).pop();
      case 'paste':
        if (_content.text.trim().isNotEmpty) {
          widget.onSubmit(
            UploadRequest.skill({
              'content': _content.text,
              'name': _name.text.trim().isEmpty ? null : _name.text.trim(),
            }),
          );
        }
      case 'git':
        if (_url.text.trim().isNotEmpty) {
          widget.onSubmit(
            UploadRequest.skill({
              'url': _url.text.trim(),
              'name': _name.text.trim().isEmpty ? null : _name.text.trim(),
            }),
          );
        }
      default:
        if (_mcp.tab == 'json') {
          final result = parseMcpConfig(
            _mcp.json.text,
            fallbackName: _mcp.name.text,
          );
          if (result.error != null || result.entries.isEmpty) {
            setState(
              () => _mcp.jsonError = i18n.t(
                'skills:upload.jsonError.${result.error ?? 'noServers'}',
              ),
            );
            return;
          }
          widget.onSubmit(UploadRequest.mcp(result.entries));
          return;
        }
        final name = _mcp.name.text.trim();
        final config = _mcp.buildConfig();
        if (name.isEmpty || config == null) return;
        widget.onSubmit(
          UploadRequest.mcp([ParsedMcpEntry(name: name, config: config)]),
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return PopScope(
      canPop: !widget.busy && !_queueBusy,
      child: SkillSheet(
        title: i18n.t('skills:upload.title'),
        subtitle: i18n.t('skills:upload.subtitle'),
        busy: widget.busy || _queueBusy,
        showCancel: _mode != 'archive',
        error: widget.error,
        canConfirm: _canSubmit,
        confirmLabel: i18n.t(
          _mode == 'archive'
              ? 'common:action.close'
              : widget.busy
              ? 'skills:upload.installing'
              : 'skills:upload.confirm',
        ),
        onConfirm: _submit,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SectionTabs(
              padding: EdgeInsets.zero,
              labels: {
                for (final mode in _modes)
                  mode: i18n.t(
                    'skills:upload.mode${mode[0].toUpperCase()}${mode.substring(1)}',
                  ),
              },
              value: _mode,
              onChanged: widget.busy || _queueBusy
                  ? null
                  : (mode) => setState(() => _mode = mode),
            ),
            Visibility(
              visible: _mode == 'archive',
              maintainState: true,
              child: ArchiveUploadQueue(
                upload: widget.onUploadArchive,
                onBusyChanged: (busy) => setState(() => _queueBusy = busy),
              ),
            ),
            if (_mode == 'paste') ...[
              SheetField(
                label: i18n.t('skills:upload.contentLabel'),
                placeholder: i18n.t('skills:upload.skillTemplate'),
                controller: _content,
                lines: 9,
                mono: true,
                onChanged: (_) => setState(() {}),
              ),
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  i18n.t('skills:upload.frontmatterHint'),
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    height: 1.6,
                    color: t.n600,
                  ),
                ),
              ),
            ],
            if (_mode == 'git') ...[
              SheetField(
                label: i18n.t('skills:upload.gitLabel'),
                placeholder: i18n.t('skills:upload.gitPlaceholder'),
                controller: _url,
                onChanged: (_) => setState(() {}),
              ),
              SheetField(
                label: i18n.t('skills:upload.nameLabel'),
                controller: _name,
              ),
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  i18n.t('skills:upload.gitHint'),
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    height: 1.6,
                    color: t.n600,
                  ),
                ),
              ),
            ],
            if (_mode == 'mcp')
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: McpConfigForm(
                  state: _mcp,
                  onChanged: () => setState(() {}),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
