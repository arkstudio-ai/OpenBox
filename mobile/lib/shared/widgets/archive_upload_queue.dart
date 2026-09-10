import 'package:dio/dio.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';
import '../i18n/i18n.dart';
import '../utils/error_text.dart';
import '../utils/format.dart';

/// Metadata is read when picking; content is streamed only when uploading.
class ArchiveSelection {
  const ArchiveSelection({
    required this.name,
    required this.size,
    required this.openRead,
    this.modified,
  });
  final String name;
  final int size;
  final DateTime? modified;
  final Stream<List<int>> Function() openRead;
  (String, int, DateTime?) get identity => (name, size, modified);
}

final archivePickerProvider =
    Provider<Future<List<ArchiveSelection>> Function()>(
      (ref) => () async {
        final files = await FilePickerPlatform.instance.pickFiles(
          type: FileType.custom,
          allowedExtensions: ['zip', 'tar', 'gz', 'tgz'],
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
      },
    );

typedef ArchiveUploader =
    Future<void> Function(
      ArchiveSelection archive,
      String? name,
      CancelToken cancel,
    );

class _Item {
  _Item(this.file);
  final ArchiveSelection file;
  String status = 'queued';
  String? error;
  String? name;
  bool started = false;
}

class ArchiveUploadQueue extends ConsumerStatefulWidget {
  const ArchiveUploadQueue({
    super.key,
    required this.upload,
    required this.onBusyChanged,
    this.zipOnly = false,
    this.allowCustomName = true,
  });
  final ArchiveUploader upload;
  final ValueChanged<bool> onBusyChanged;
  final bool zipOnly;
  final bool allowCustomName;
  @override
  ConsumerState<ArchiveUploadQueue> createState() => _ArchiveUploadQueueState();
}

class _ArchiveUploadQueueState extends ConsumerState<ArchiveUploadQueue> {
  final _items = <_Item>[];
  final _name = TextEditingController();
  final _queueScroll = ScrollController();
  final _cancel = CancelToken();
  bool _busy = false;
  bool _picking = false;
  bool _started = false;
  String? _error;

  @override
  void dispose() {
    _cancel.cancel();
    _name.dispose();
    _queueScroll.dispose();
    super.dispose();
  }

  void _setBusy(bool value) {
    if (!mounted) return;
    setState(() => _busy = value);
    widget.onBusyChanged(value);
  }

  Future<void> _pick() async {
    if (_busy) return;
    _picking = true;
    _setBusy(true);
    try {
      final files = await ref.read(archivePickerProvider)();
      if (!mounted) return;
      final seen = _items.map((item) => item.file.identity).toSet();
      final incoming = files.where((file) => seen.add(file.identity)).toList();
      final size = [
        ..._items.map((item) => item.file),
        ...incoming,
      ].fold<int>(0, (sum, file) => sum + file.size);
      final i18n = ref.read(i18nProvider);
      if (_items.length + incoming.length > 20 ||
          size > 128 * 1024 * 1024 ||
          incoming.any(
            (file) => file.size > 32 * 1024 * 1024 || file.size < 0,
          )) {
        setState(() => _error = i18n.t('common:archiveQueue.limits'));
        return;
      }
      if (incoming.any(
        (file) => !RegExp(
          widget.zipOnly ? r'\.zip$' : r'\.(zip|tar|tar\.gz|tgz)$',
          caseSensitive: false,
        ).hasMatch(file.name),
      )) {
        setState(
          () => _error = i18n.t(
            widget.zipOnly
                ? 'admin:mobile.archiveOnlyZip'
                : 'skills:upload.archiveHint',
          ),
        );
        return;
      }
      setState(() {
        _items.addAll(incoming.map(_Item.new));
        _error = null;
      });
    } catch (error) {
      if (mounted) {
        setState(() => _error = errorText(ref.read(i18nProvider), error));
      }
    } finally {
      _picking = false;
      _setBusy(false);
    }
  }

  Future<void> _submit() async {
    if (_busy) return;
    final pending = _items
        .where((item) => item.status == 'queued' || item.status == 'failed')
        .toList();
    if (pending.isEmpty) return;
    final customName = widget.allowCustomName && !_started && _items.length == 1
        ? _name.text.trim()
        : '';
    _started = true;
    _setBusy(true);
    try {
      for (final item in pending) {
        if (!mounted || _cancel.isCancelled) break;
        if (!item.started) {
          item.name = customName.isEmpty ? null : customName;
          item.started = true;
        }
        setState(() {
          item.status = 'working';
          item.error = null;
        });
        try {
          await widget.upload(item.file, item.name, _cancel);
          if (!mounted || _cancel.isCancelled) break;
          setState(() => item.status = 'success');
        } catch (error) {
          if (!mounted || _cancel.isCancelled) break;
          final i18n = ref.read(i18nProvider);
          setState(() {
            item.status = 'failed';
            item.error = error is DioException && error.response == null
                ? i18n.t('common:archiveQueue.unknown')
                : errorText(i18n, error);
          });
        }
      }
    } finally {
      _setBusy(false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final pending = _items
        .where((item) => item.status == 'queued' || item.status == 'failed')
        .length;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const SizedBox(height: 12),
        OutlinedButton.icon(
          onPressed: _busy ? null : _pick,
          icon: const Icon(Icons.folder_open),
          label: Text(i18n.t('common:archiveQueue.pick')),
        ),
        Text(
          i18n.t('common:archiveQueue.limits'),
          style: TextStyle(color: t.n600, fontSize: FontSizes.sm),
        ),
        if (_error != null) Text(_error!, style: TextStyle(color: t.danger)),
        if (_items.isNotEmpty) ...[
          Padding(
            padding: const EdgeInsets.only(top: 12, bottom: 4),
            child: Text(
              '${_items.length} / 20',
              style: TextStyle(color: t.n600, fontSize: FontSizes.xs),
            ),
          ),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 240),
            child: Scrollbar(
              controller: _queueScroll,
              thumbVisibility: true,
              child: SingleChildScrollView(
                controller: _queueScroll,
                primary: false,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    for (final item in _items)
                      Container(
                        padding: const EdgeInsets.symmetric(vertical: 8),
                        decoration: BoxDecoration(
                          border: Border(bottom: BorderSide(color: t.hair)),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Row(
                              children: [
                                Icon(
                                  switch (item.status) {
                                    'working' => Icons.upload_outlined,
                                    'success' => Icons.check_circle_outline,
                                    'failed' => Icons.error_outline,
                                    _ => Icons.insert_drive_file_outlined,
                                  },
                                  size: 18,
                                  color: item.status == 'failed'
                                      ? t.danger
                                      : t.n600,
                                ),
                                const SizedBox(width: 10),
                                Expanded(
                                  child: Text(
                                    item.file.name,
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                      fontSize: FontSizes.md,
                                    ),
                                  ),
                                ),
                                IconButton(
                                  onPressed: _busy
                                      ? null
                                      : () =>
                                            setState(() => _items.remove(item)),
                                  tooltip: i18n.t(
                                    'common:archiveQueue.remove',
                                    vars: {'name': item.file.name},
                                  ),
                                  icon: const Icon(Icons.close, size: 18),
                                ),
                              ],
                            ),
                            Wrap(
                              spacing: 6,
                              children: [
                                Text(
                                  i18n.t('common:archiveQueue.${item.status}'),
                                  style: TextStyle(
                                    color: item.status == 'failed'
                                        ? t.danger
                                        : t.n600,
                                    fontSize: FontSizes.xs,
                                  ),
                                ),
                                Text(
                                  '· ${formatBytes(item.file.size)}',
                                  style: TextStyle(
                                    color: t.n600,
                                    fontSize: FontSizes.xs,
                                  ),
                                ),
                              ],
                            ),
                            if (item.error != null)
                              Text(
                                item.error!,
                                style: TextStyle(color: t.danger, fontSize: 12),
                              ),
                          ],
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ),
        ],
        const SizedBox(height: 12),
        if (widget.allowCustomName) ...[
          TextField(
            controller: _name,
            enabled: !_busy && !_started && _items.length <= 1,
            decoration: InputDecoration(
              labelText: i18n.t('skills:upload.nameLabel'),
            ),
          ),
          Text(
            i18n.t('skills:upload.batchNameHint'),
            style: TextStyle(color: t.n600, fontSize: 12),
          ),
        ],
        const SizedBox(height: 8),
        FilledButton(
          key: const ValueKey('submit-archives'),
          onPressed: _busy || pending == 0 ? null : _submit,
          child: Text(
            i18n.t(
              'common:archiveQueue.${_busy && !_picking ? 'working' : 'submit'}',
              count: pending,
            ),
          ),
        ),
      ],
    );
  }
}
