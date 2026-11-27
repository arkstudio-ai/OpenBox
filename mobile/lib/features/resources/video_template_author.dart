import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/api/providers.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/json.dart';
import '../../shared/models/resource.dart';
import '../../shared/utils/error_text.dart';

class VideoTemplateAuthorPage extends ConsumerStatefulWidget {
  const VideoTemplateAuthorPage({super.key, required this.source});
  final Resource source;

  @override
  ConsumerState<VideoTemplateAuthorPage> createState() =>
      _VideoTemplateAuthorState();
}

class _VideoTemplateAuthorState extends ConsumerState<VideoTemplateAuthorPage> {
  final title = TextEditingController(), direction = TextEditingController();
  Map<String, dynamic>? draft;
  String pack = '', error = '';
  bool busy = false, share = false;

  @override
  void dispose() {
    title.dispose();
    direction.dispose();
    super.dispose();
  }

  Future<void> perform(String action) async {
    setState(() {
      busy = true;
      error = '';
    });
    final dio = ref.read(apiDioProvider);
    try {
      const path = '/api/video-templates';
      if (action == 'analyze') {
        final response = await dio.post<Map<String, dynamic>>(
          '$path/sources/${Uri.encodeComponent(widget.source.id)}/analyze',
        );
        if (mounted) {
          direction.text = asString(response.data?['direction']) ?? '';
        }
      } else if (action == 'save') {
        final response = await dio.post<Map<String, dynamic>>(
          '$path/drafts',
          data: {
            'asset_id': widget.source.id,
            'title': title.text,
            'direction': direction.text,
            'reference_pack': pack,
          },
        );
        if (mounted) {
          setState(() {
            draft = response.data;
            share = false;
          });
        }
      } else {
        final pin = asMap(draft?['template_ref']);
        final response = await dio.post<Map<String, dynamic>>(
          '$path/${Uri.encodeComponent(pin['edition_id'] as String)}/publish',
          data: {
            'sha256': pin['sha256'],
            'share_finished_preview': share,
            'share_cast_images': share && asList(draft?['cast']).isNotEmpty,
          },
        );
        if (mounted) setState(() => draft = response.data);
      }
    } catch (e) {
      if (mounted) setState(() => error = errorText(ref.read(i18nProvider), e));
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    String tr(String key) => i18n.t('resources:templates.$key');
    final data = draft;
    final status = asString(data?['status']);
    return Scaffold(
      appBar: AppBar(title: Text(tr('make'))),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            Text(
              widget.source.name,
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 12),
            if (data == null) ...[
              TextField(
                controller: title,
                enabled: !busy,
                maxLength: 88,
                onChanged: (_) => setState(() {}),
                decoration: InputDecoration(labelText: tr('title')),
              ),
              TextField(
                controller: direction,
                enabled: !busy,
                minLines: 4,
                maxLines: 10,
                maxLength: 5500,
                onChanged: (_) => setState(() {}),
                decoration: InputDecoration(labelText: tr('direction')),
              ),
              OutlinedButton(
                onPressed: busy ? null : () => perform('analyze'),
                child: Text(tr('analyze')),
              ),
              DropdownButtonFormField<String>(
                initialValue: pack,
                isExpanded: true,
                decoration: InputDecoration(labelText: tr('pack')),
                items: [
                  DropdownMenuItem(value: '', child: Text(tr('none'))),
                  DropdownMenuItem(
                    value: 'gallery-stage',
                    child: Text(tr('stage'), overflow: TextOverflow.ellipsis),
                  ),
                ],
                onChanged: busy ? null : (v) => setState(() => pack = v ?? ''),
              ),
              FilledButton(
                onPressed:
                    !busy &&
                        title.text.trim().length >= 2 &&
                        direction.text.trim().length >= 20
                    ? () => perform('save')
                    : null,
                child: Text(tr('save')),
              ),
            ] else ...[
              Text(
                '${data['title']} · V${asMap(data['template_ref'])['version']}',
              ),
              Text(
                i18n.t(
                  'resources:templates.timing',
                  count: asList(asMap(data['rhythm'])['cuts']).length,
                  vars: {'seconds': asMap(data['rhythm'])['seconds']},
                ),
              ),
              Text(asString(data['direction']) ?? ''),
              for (final p in asList(data['cast']))
                Text(asString(asMap(p)['label']) ?? ''),
              Text(tr('state.$status')),
              if (status == 'unpublished' || status == 'withdrawn') ...[
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text(
                    tr(
                      asList(data['cast']).isEmpty ? 'consent' : 'castConsent',
                    ),
                  ),
                  value: share,
                  onChanged: busy
                      ? null
                      : (v) => setState(() => share = v == true),
                ),
                FilledButton(
                  onPressed: !busy && share ? () => perform('publish') : null,
                  child: Text(tr('publish')),
                ),
                TextButton(
                  onPressed: busy ? null : () => setState(() => draft = null),
                  child: Text(tr('edit')),
                ),
              ],
            ],
            if (busy) const LinearProgressIndicator(),
            if (error.isNotEmpty)
              Text(
                error,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
          ],
        ),
      ),
    );
  }
}
