import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../api/assistant_api.dart';

/// Opening the review only reads the request. A separate signed receipt records
/// actual foreground display; neither endpoint answers or approves anything.
class AssistantRequestReviewButton extends ConsumerWidget {
  const AssistantRequestReviewButton({
    super.key,
    required this.scope,
    required this.kind,
    required this.requestId,
    required this.binding,
  });
  final AssistantScope scope;
  final String kind;
  final String requestId;
  final Map<String, dynamic> binding;

  @override
  Widget build(BuildContext context, WidgetRef ref) => TextButton(
    onPressed: ref.watch(assistantScopeProvider) != scope
        ? null
        : () {
            FocusScope.of(context).unfocus();
            unawaited(
              showModalBottomSheet<void>(
                context: context,
                isScrollControlled: true,
                useSafeArea: true,
                builder: (_) => AssistantRequestReview(
                  scope: scope,
                  kind: kind,
                  requestId: requestId,
                  binding: Map.unmodifiable(binding),
                ),
              ),
            );
          },
    child: Text(ref.watch(i18nProvider).t('chat:assistant.requests.review')),
  );
}

class AssistantRequestReview extends ConsumerStatefulWidget {
  const AssistantRequestReview({
    super.key,
    required this.scope,
    required this.kind,
    required this.requestId,
    required this.binding,
  });
  final AssistantScope scope;
  final String kind;
  final String requestId;
  final Map<String, dynamic> binding;

  @override
  ConsumerState<AssistantRequestReview> createState() =>
      _AssistantRequestReviewState();
}

class _AssistantRequestReviewState extends ConsumerState<AssistantRequestReview>
    with WidgetsBindingObserver {
  final _viewport = GlobalKey();
  final _scroll = ScrollController();
  final _keys = <GlobalKey>[];
  final _coverage = <_Coverage>[];
  List<String> _segments = [];
  String? _token;
  Object? _error;
  bool _loading = true, _submitting = false, _displayed = false;
  bool _checkScheduled = false;
  int _epoch = 0;
  Timer? _timer, _expiry;
  Size? _viewportSize;
  Object? _layout;

  bool get _current =>
      mounted && ref.read(assistantScopeProvider) == widget.scope;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _scroll.addListener(_schedule);
    // Also observe the final sheet/route animation frame and route uncovering.
    _timer = Timer.periodic(
      const Duration(milliseconds: 250),
      (_) => _schedule(),
    );
    ref.listenManual(assistantScopeProvider, (previous, next) {
      if (previous != next) _invalidate();
    });
    Future.microtask(_load);
  }

  @override
  void didUpdateWidget(AssistantRequestReview oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.scope != widget.scope ||
        oldWidget.kind != widget.kind ||
        oldWidget.requestId != widget.requestId ||
        oldWidget.binding['request_revision'] !=
            widget.binding['request_revision']) {
      unawaited(_load());
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _schedule();
  }

  void _invalidate() {
    if (!mounted) return;
    _epoch++;
    _expiry?.cancel();
    setState(() {
      _segments = [];
      _token = null;
      _keys.clear();
      _coverage.clear();
      _viewportSize = null;
      _loading = _submitting = _displayed = false;
    });
  }

  Future<void> _load() async {
    if (!_current) return;
    _invalidate();
    final epoch = _epoch;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      if (widget.binding['workspace_id'] != widget.scope.workspaceId ||
          !const {'question', 'permission'}.contains(widget.kind) ||
          widget.binding['assistant_session_id'] is! String ||
          widget.binding['request_revision'] is! String ||
          (widget.binding['request_revision'] as String).length != 64) {
        throw StateError('Request scope changed');
      }
      // Start at dispatch, so a delayed response never extends the server's
      // five-minute display window. The server still verifies its own expiry.
      _expiry = Timer(const Duration(minutes: 5), _invalidate);
      final data = await ref
          .read(assistantApiProvider(widget.scope))
          .reviewRequest(widget.kind, widget.requestId);
      if (!_current || epoch != _epoch) return;
      final segments = data['segments'];
      final token = data['display_token'];
      if (data['request_revision'] != widget.binding['request_revision'] ||
          data['request_revision'] is! String ||
          segments is! List ||
          segments.isEmpty ||
          segments.any((s) => s is! String) ||
          token is! String ||
          token.isEmpty ||
          token.length > 4096) {
        throw const FormatException('Unconfirmed request review');
      }
      final text = segments.cast<String>();
      if (text.fold<int>(
            0,
            (length, segment) => length + segment.runes.length,
          ) >
          32000) {
        throw const FormatException('Unbounded request review');
      }
      setState(() {
        _segments = List.unmodifiable(text);
        _token = token;
        _keys.addAll(_segments.map((_) => GlobalKey()));
        _coverage.addAll(_segments.map((_) => _Coverage()));
        _loading = false;
      });
      _schedule();
    } catch (error) {
      if (_current && epoch == _epoch) {
        _invalidate();
        setState(() => _error = error);
      }
    }
  }

  void _schedule() {
    if (_checkScheduled ||
        !_current ||
        _token == null ||
        _submitting ||
        _displayed ||
        WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed) {
      return;
    }
    _checkScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _checkScheduled = false;
      _check();
    });
    WidgetsBinding.instance.ensureVisualUpdate();
  }

  void _check() {
    if (!_current || _token == null || _submitting || _displayed) return;
    final route = ModalRoute.of(context);
    if (WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed ||
        route?.isCurrent != true ||
        route?.animation?.status != AnimationStatus.completed ||
        route?.secondaryAnimation?.status != AnimationStatus.dismissed) {
      return;
    }
    final viewport = _viewport.currentContext?.findRenderObject();
    if (viewport is! RenderBox || !viewport.hasSize) return;
    final boxes = _keys.map((key) => key.currentContext?.findRenderObject());
    if (boxes.any((box) => box is! RenderBox || !box.hasSize)) return;
    final rendered = boxes.cast<RenderBox>().toList();
    // Resizing or changing text scale invalidates previously measured slices.
    if (_viewportSize != viewport.size ||
        rendered.indexed.any(
          (entry) => _coverage[entry.$1].size != entry.$2.size,
        )) {
      _viewportSize = viewport.size;
      for (final (index, box) in rendered.indexed) {
        _coverage[index] = _Coverage()..size = box.size;
      }
    }
    final visible = viewport.localToGlobal(Offset.zero) & viewport.size;
    for (final (index, box) in rendered.indexed) {
      _coverage[index].observe(
        box.localToGlobal(Offset.zero) & box.size,
        visible,
      );
    }
    if (_coverage.isNotEmpty && _coverage.every((item) => item.complete)) {
      unawaited(_record(_token!));
    }
  }

  Future<void> _record(String token) async {
    if (!_current || _submitting) return;
    final epoch = _epoch;
    setState(() => _submitting = true);
    _expiry?.cancel();
    _expiry = Timer(const Duration(minutes: 5), _invalidate);
    try {
      final receipt = await ref
          .read(assistantApiProvider(widget.scope))
          .requestDisplayed(token);
      if (!_current || epoch != _epoch) return;
      if (receipt['state'] != 'displayed' ||
          receipt['display_id'] is! String ||
          (receipt['display_id'] as String).isEmpty) {
        throw const FormatException('Unconfirmed display receipt');
      }
      setState(() => _displayed = true);
    } catch (error) {
      if (_current && epoch == _epoch) {
        _invalidate();
        setState(() => _error = error);
      }
    } finally {
      if (_current && epoch == _epoch) {
        setState(() => _submitting = false);
      }
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    _expiry?.cancel();
    _scroll.dispose();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final current = ref.watch(assistantScopeProvider) == widget.scope;
    final layout = (
      MediaQuery.of(context),
      DefaultTextStyle.of(context).style,
      Directionality.of(context),
      Theme.of(context).textTheme,
    );
    if (_layout != layout) {
      _layout = layout;
      _viewportSize = null;
    }
    return SafeArea(
      child: FractionallySizedBox(
        heightFactor: .85,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            children: [
              Align(
                alignment: AlignmentDirectional.centerEnd,
                child: CloseButton(
                  onPressed: () => Navigator.of(context).pop(),
                ),
              ),
              Expanded(
                child: ClipRect(
                  key: _viewport,
                  child: !current
                      ? const SizedBox.shrink()
                      : SingleChildScrollView(
                          controller: _scroll,
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              Text(
                                i18n.t('chat:assistant.requests.reviewHint'),
                              ),
                              if (_loading)
                                const Center(
                                  child: CircularProgressIndicator(),
                                ),
                              if (_error case final error?)
                                Text(errorText(i18n, error)),
                              if (!_loading && _token == null)
                                TextButton(
                                  onPressed: _load,
                                  child: Text(i18n.t('chat:assistant.reload')),
                                ),
                              for (final (index, segment) in _segments.indexed)
                                Text(
                                  segment,
                                  key: _keys[index],
                                  style: const TextStyle(
                                    fontFamily: 'monospace',
                                  ),
                                ),
                              if (_submitting) const LinearProgressIndicator(),
                              if (_displayed)
                                Text(
                                  i18n.t('chat:assistant.requests.reviewed'),
                                ),
                            ],
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

/// Retain all vertical slices actually painted inside the dedicated clipped
/// viewport. Even a single wrapped block taller than the screen must be fully
/// traversed; jumping past the middle cannot certify the omitted text.
class _Coverage {
  Size? size;
  final _ranges = <({double start, double end})>[];
  bool get complete =>
      _ranges.length == 1 &&
      _ranges.single.start <= .01 &&
      _ranges.single.end >= (size?.height ?? double.infinity) - .01;

  void observe(Rect bounds, Rect viewport) {
    if (bounds.width == 0 ||
        bounds.height == 0 ||
        bounds.left < viewport.left - .01 ||
        bounds.right > viewport.right + .01 ||
        !bounds.overlaps(viewport)) {
      return;
    }
    final seen = bounds.intersect(viewport).shift(-bounds.topLeft);
    _ranges.add((start: seen.top, end: seen.bottom));
    _ranges.sort((a, b) => a.start.compareTo(b.start));
    for (var index = 1; index < _ranges.length;) {
      final previous = _ranges[index - 1], next = _ranges[index];
      if (next.start <= previous.end + .01) {
        _ranges[index - 1] = (
          start: previous.start,
          end: next.end > previous.end ? next.end : previous.end,
        );
        _ranges.removeAt(index);
      } else {
        index++;
      }
    }
  }
}
