import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/platform_accounts_api.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/platform_account.dart';
import '../state/auth_center_providers.dart';
import 'auth_widgets.dart';
import 'publish_job_view.dart';

class PublishSheet extends ConsumerStatefulWidget {
  const PublishSheet({super.key, required this.scope, required this.onClose});
  final PlatformScope scope;
  final VoidCallback onClose;
  @override
  ConsumerState<PublishSheet> createState() => _PublishSheetState();
}

class _PublishSheetState extends ConsumerState<PublishSheet> {
  final _title = TextEditingController();
  final _tags = TextEditingController();
  final _form = GlobalKey<FormState>();
  final _cancel = CancelToken();
  String? _assetId;
  int _privacy = 0;
  bool _busy = false;
  Object? _error;
  PublishResult? _result;

  @override
  void dispose() {
    _cancel.cancel();
    _title.dispose();
    _tags.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_busy ||
        _result != null ||
        _assetId == null ||
        !_form.currentState!.validate()) {
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final result = await ref
          .read(platformAccountsApiProvider)
          .publish(
            widget.scope,
            assetId: _assetId!,
            title: _title.text.trim(),
            hashtags: parsePublishHashtags(_tags.text),
            privacy: _privacy,
            cancel: _cancel,
          );
      if (!mounted) return;
      setState(() => _result = result);
      ref.invalidate(publishJobsProvider(widget.scope));
    } catch (error) {
      if (mounted && !_cancel.isCancelled) {
        setState(() => _error = error);
        // Never retry this POST automatically: the server may have created a job.
        ref.invalidate(publishJobsProvider(widget.scope));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final videos = ref.watch(publishVideosProvider(widget.scope));
    final eligible =
        videos.valueOrNull?.items.where(isPublishableVideo).toList() ?? [];
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          20,
          8,
          20,
          MediaQuery.viewInsetsOf(context).bottom + 20,
        ),
        child: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      i18n.t('auth-center:publish.title'),
                      style: TextStyle(
                        fontSize: 20,
                        fontWeight: FontWeight.w600,
                        color: t.ink,
                      ),
                    ),
                  ),
                  IconButton(
                    onPressed: widget.onClose,
                    tooltip: i18n.t('common:action.close'),
                    icon: const Icon(Icons.close),
                  ),
                ],
              ),
              if (_result != null)
                PublishJobView(
                  key: ValueKey(_result!.job.id),
                  scope: widget.scope,
                  jobId: _result!.job.id,
                  result: _result,
                  onChanged: () =>
                      ref.invalidate(publishJobsProvider(widget.scope)),
                )
              else
                Form(
                  key: _form,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(i18n.t('auth-center:mobile.publishIntro')),
                      const SizedBox(height: 16),
                      if (videos.isLoading && !videos.hasValue)
                        const Center(child: CircularProgressIndicator())
                      else if (videos.hasError)
                        AuthError(
                          error: videos.error!,
                          retry: () => ref.invalidate(
                            publishVideosProvider(widget.scope),
                          ),
                        )
                      else if (eligible.isEmpty)
                        Text(i18n.t('auth-center:publish.noVideos'))
                      else
                        DropdownButtonFormField<String>(
                          initialValue: eligible.any((v) => v.id == _assetId)
                              ? _assetId
                              : null,
                          isExpanded: true,
                          decoration: InputDecoration(
                            labelText: i18n.t('auth-center:publish.video'),
                          ),
                          items: [
                            for (final video in eligible)
                              DropdownMenuItem(
                                value: video.id,
                                child: Text(
                                  '${video.name} · ${(video.size / 1048576).toStringAsFixed(1)} MB',
                                  overflow: TextOverflow.ellipsis,
                                ),
                              ),
                          ],
                          onChanged: _busy
                              ? null
                              : (id) => setState(() => _assetId = id),
                          validator: (value) => value == null
                              ? i18n.t('auth-center:publish.pickVideo')
                              : null,
                        ),
                      const SizedBox(height: 12),
                      TextFormField(
                        controller: _title,
                        enabled: !_busy,
                        maxLength: 55,
                        decoration: InputDecoration(
                          labelText: i18n.t('auth-center:publish.caption'),
                          hintText: i18n.t(
                            'auth-center:publish.captionPlaceholder',
                          ),
                        ),
                        validator: (value) => (value ?? '').runes.length > 55
                            ? i18n.t('auth-center:publish.captionPlaceholder')
                            : null,
                      ),
                      TextFormField(
                        controller: _tags,
                        enabled: !_busy,
                        maxLength: 300,
                        decoration: InputDecoration(
                          labelText: i18n.t('auth-center:publish.hashtags'),
                          hintText: i18n.t(
                            'auth-center:publish.hashtagsPlaceholder',
                          ),
                        ),
                      ),
                      DropdownButtonFormField<int>(
                        initialValue: _privacy,
                        decoration: InputDecoration(
                          labelText: i18n.t('auth-center:publish.privacy'),
                        ),
                        items: [
                          for (final item in const {
                            0: 'privacyPublic',
                            2: 'privacyFriends',
                            1: 'privacyPrivate',
                          }.entries)
                            DropdownMenuItem(
                              value: item.key,
                              child: Text(
                                i18n.t('auth-center:publish.${item.value}'),
                              ),
                            ),
                        ],
                        onChanged: _busy
                            ? null
                            : (value) => setState(() => _privacy = value ?? 0),
                      ),
                      const SizedBox(height: 16),
                      if (_error != null) ...[
                        Text(
                          platformErrorText(i18n, _error!),
                          style: TextStyle(color: t.danger),
                        ),
                        Text(i18n.t('auth-center:mobile.createUncertain')),
                        const SizedBox(height: 12),
                      ],
                      FilledButton(
                        onPressed: _busy || _assetId == null || videos.hasError
                            ? null
                            : _submit,
                        child: _busy
                            ? const SizedBox(
                                width: 20,
                                height: 20,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : Text(i18n.t('auth-center:publish.generate')),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
