import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/router/paths.dart';
import '../api/assistant_api.dart';
import 'markdown_view.dart';

class AssistantReport extends ConsumerStatefulWidget {
  const AssistantReport({
    super.key,
    required this.scope,
    required this.resultId,
  });
  final AssistantScope scope;
  final String resultId;
  @override
  ConsumerState<AssistantReport> createState() => _AssistantReportState();
}

class _AssistantReportState extends ConsumerState<AssistantReport>
    with WidgetsBindingObserver {
  final _pages = <Map<String, dynamic>>[];
  bool _loading = true;
  bool _failed = false;
  bool _inFlight = false;
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
  void _hideUnverified() {
    if (!_current) return;
    _authorityEpoch++;
    setState(() {
      _pages.clear();
      _loading = false;
      _failed = true;
    });
  }

  Future<void> _load({bool recheck = false}) async {
    if (!_current || _inFlight) return;
    _inFlight = true;
    final epoch = _authorityEpoch;
    setState(() {
      _loading = true;
      _failed = false;
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
    } catch (_) {
      _hideUnverified();
    } finally {
      _inFlight = false;
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final current = ref.watch(assistantScopeProvider) == widget.scope;
    return SafeArea(
      child: FractionallySizedBox(
        heightFactor: .85,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: !current
              ? const SizedBox.shrink()
              : ListView(
                  children: [
                    Text(
                      i18n.t('chat:assistant.originalReport'),
                      style: Theme.of(context).textTheme.titleMedium,
                    ),
                    if (_failed)
                      Text(i18n.t('chat:assistant.sourceUnavailable')),
                    if (!_loading)
                      for (final page in _pages)
                        CopyAuthority(
                          check: (text) async {
                            final epoch = _authorityEpoch;
                            try {
                              final fresh = await ref
                                  .read(assistantApiProvider(widget.scope))
                                  .result(
                                    widget.resultId,
                                    offset: asInt(page['offset']) ?? 0,
                                    version: asString(page['source_version']),
                                  );
                              if (!_current || epoch != _authorityEpoch) {
                                return false;
                              }
                              final available = asList(fresh['sources']).any(
                                (s) =>
                                    asString(
                                      asMap(s)['text'],
                                    )?.contains(text) ==
                                    true,
                              );
                              if (!available) _hideUnverified();
                              return available;
                            } catch (_) {
                              _hideUnverified();
                              return false;
                            }
                          },
                          child: Column(
                            children: [
                              for (final source in asList(page['sources']))
                                Column(
                                  crossAxisAlignment:
                                      CrossAxisAlignment.stretch,
                                  children: [
                                    if (asMap(source)['session_id'] is String)
                                      TextButton(
                                        onPressed: () => context.push(
                                          Paths.chat(
                                            asMap(source)['session_id']
                                                as String,
                                          ),
                                        ),
                                        child: Text(
                                          i18n.t('chat:assistant.openSource'),
                                        ),
                                      ),
                                    MarkdownView(
                                      asString(asMap(source)['text']) ?? '',
                                    ),
                                  ],
                                ),
                            ],
                          ),
                        ),
                    if (_loading)
                      const Center(child: CircularProgressIndicator())
                    else if (_failed ||
                        _pages.lastOrNull?['next_offset'] != null)
                      TextButton(
                        onPressed: _load,
                        child: Text(
                          i18n.t(
                            _failed
                                ? 'chat:assistant.reload'
                                : 'chat:assistant.moreReport',
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
