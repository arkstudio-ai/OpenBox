import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/utils/error_text.dart';
import '../api/assistant_api.dart';
import 'markdown_view.dart';

/// A task conversation's own final reply, read on demand under its card
/// (web `AssistantTaskCard` → `FullResult`). Loaded pages are re-read before
/// more are added, on a slow timer and on resume; a page that can no longer
/// be read takes everything shown with it, and copying re-reads first.
class AssistantFullResult extends ConsumerStatefulWidget {
  const AssistantFullResult({
    super.key,
    required this.scope,
    required this.resultId,
  });
  final AssistantScope scope;
  final String resultId;
  @override
  ConsumerState<AssistantFullResult> createState() =>
      _AssistantFullResultState();
}

class _AssistantFullResultState extends ConsumerState<AssistantFullResult>
    with WidgetsBindingObserver {
  final _pages = <Map<String, dynamic>>[];
  bool _loading = true;
  bool _failed = false;
  bool _inFlight = false;
  Object? _error;
  int _authorityEpoch = 0;
  Timer? _timer;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _timer = Timer.periodic(const Duration(seconds: 15), (_) {
      if (WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed) {
        unawaited(_load(recheck: true));
      }
    });
    Future.microtask(_load);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) unawaited(_load(recheck: true));
  }

  @override
  void dispose() {
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  bool get _current =>
      mounted && ref.read(assistantScopeProvider) == widget.scope;
  void _hideUnverified([Object? error]) {
    if (!_current) return;
    _authorityEpoch++;
    setState(() {
      _pages.clear();
      _loading = false;
      _failed = true;
      _error = error;
    });
  }

  Future<void> _load({bool recheck = false}) async {
    if (!_current || _inFlight) return;
    _inFlight = true;
    final epoch = _authorityEpoch;
    setState(() {
      _loading = true;
      _failed = false;
      _error = null;
    });
    try {
      final checked = <Map<String, dynamic>>[];
      for (final page in _pages) {
        checked.add(
          await ref
              .read(assistantApiProvider(widget.scope))
              .result(
                widget.resultId,
                offset: asInt(page['offset']) ?? 0,
                version: asString(page['source_version']),
              ),
        );
        if (!_current || epoch != _authorityEpoch) return;
      }
      if (recheck) {
        if (_current) {
          setState(() {
            _pages
              ..clear()
              ..addAll(checked);
            _loading = false;
          });
        }
        return;
      }
      final previous = _pages.lastOrNull;
      final page = await ref
          .read(assistantApiProvider(widget.scope))
          .result(
            widget.resultId,
            offset: asInt(previous?['next_offset']) ?? 0,
            version: asString(previous?['source_version']),
          );
      if (_current && epoch == _authorityEpoch) {
        setState(() {
          _pages
            ..clear()
            ..addAll(checked);
          _pages.add(page);
          _loading = false;
        });
      }
    } catch (error) {
      _hideUnverified(error);
    } finally {
      _inFlight = false;
    }
  }

  Future<bool> _canCopy(Map<String, dynamic> page, String text) async {
    final epoch = _authorityEpoch;
    try {
      final fresh = await ref
          .read(assistantApiProvider(widget.scope))
          .result(
            widget.resultId,
            offset: asInt(page['offset']) ?? 0,
            version: asString(page['source_version']),
          );
      if (!_current || epoch != _authorityEpoch) return false;
      final available = asList(
        fresh['sources'],
      ).any((s) => asString(asMap(s)['text'])?.contains(text) == true);
      if (!available) _hideUnverified();
      return available;
    } catch (error) {
      _hideUnverified(error);
      return false;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    if (ref.watch(assistantScopeProvider) != widget.scope) {
      return const SizedBox.shrink();
    }
    final muted = TextStyle(fontSize: FontSizes.sm, color: t.n600);
    final sources = [
      for (final page in _pages)
        for (final source in asList(page['sources'])) asMap(source),
    ];
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(top: 12),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.md),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (_failed) ...[
            Text(
              _error == null
                  ? i18n.t('common:state.unavailable')
                  : errorText(i18n, _error!),
              style: TextStyle(fontSize: FontSizes.sm, color: t.dangerInk),
            ),
            _LinkButton(
              label: i18n.t('chat:assistant.reload'),
              onTap: _inFlight ? null : _load,
            ),
          ] else if (_pages.isEmpty)
            Semantics(
              liveRegion: true,
              child: Text(
                i18n.t('chat:assistant.card.loadingResult'),
                style: muted,
              ),
            )
          else ...[
            if (sources.isEmpty)
              Text(i18n.t('chat:assistant.card.noResultText'), style: muted),
            for (final page in _pages)
              CopyAuthority(
                check: (text) => _canCopy(page, text),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    for (final source in asList(page['sources']))
                      MarkdownView(asString(asMap(source)['text']) ?? ''),
                  ],
                ),
              ),
            if (_loading)
              const Padding(
                padding: EdgeInsets.only(top: 8),
                child: LinearProgressIndicator(minHeight: 2),
              )
            else if (_pages.lastOrNull?['next_offset'] != null)
              _LinkButton(
                label: i18n.t('chat:assistant.card.moreResult'),
                onTap: _load,
              ),
          ],
        ],
      ),
    );
  }
}

class _LinkButton extends StatelessWidget {
  const _LinkButton({required this.label, required this.onTap});
  final String label;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return TextButton(
      onPressed: onTap,
      style: TextButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 0, vertical: 4),
        minimumSize: Size.zero,
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
        foregroundColor: t.n700,
      ),
      child: Text(
        label,
        style: const TextStyle(
          fontSize: FontSizes.sm,
          decoration: TextDecoration.underline,
        ),
      ),
    );
  }
}
