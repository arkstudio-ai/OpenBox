import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../api/private_browser_api.dart';
import '../state/private_browser_controller.dart';

class PrivateBrowserPage extends StatelessWidget {
  const PrivateBrowserPage({super.key, required this.scope});
  final PrivateBrowserScope scope;
  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: context.tokens.bg,
    appBar: AppBar(
      title: Consumer(
        builder: (context, ref, _) =>
            Text(ref.watch(i18nProvider).t('workbench:privateBrowser.title')),
      ),
    ),
    body: PrivateBrowserTab(key: ValueKey(scope), scope: scope),
  );
}

/// A finite screenshot/input surface, never a native desktop or embedded CDP.
class PrivateBrowserTab extends ConsumerStatefulWidget {
  const PrivateBrowserTab({super.key, required this.scope});
  final PrivateBrowserScope scope;
  @override
  ConsumerState<PrivateBrowserTab> createState() => _PrivateBrowserTabState();
}

class _PrivateBrowserTabState extends ConsumerState<PrivateBrowserTab>
    with WidgetsBindingObserver {
  final _url = TextEditingController(), _text = TextEditingController();
  String _key = 'Enter', _mouseButton = 'left';
  bool _routeVisible = true;
  PrivateBrowserController? _mountedController;
  PrivateBrowserController get controller =>
      ref.read(privateBrowserControllerProvider(widget.scope));

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _routeVisible = ModalRoute.of(context)?.isCurrent ?? true;
    _mountedController = controller;
    final lifecycle = WidgetsBinding.instance.lifecycleState;
    // ModalRoute's inherited status updates on push/pop even while this view
    // remains mounted. Stop synchronously, without notifying during build.
    controller.setVisible(
      _routeVisible &&
          (lifecycle == null || lifecycle == AppLifecycleState.resumed),
      notify: false,
    );
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    controller.setVisible(state == AppLifecycleState.resumed && _routeVisible);
  }

  @override
  void dispose() {
    _mountedController?.setVisible(false, notify: false);
    WidgetsBinding.instance.removeObserver(this);
    _url.dispose();
    _text.dispose();
    super.dispose();
  }

  bool get _validUrl {
    final uri = Uri.tryParse(_url.text);
    return uri != null &&
        {'http', 'https'}.contains(uri.scheme) &&
        uri.host.isNotEmpty &&
        _url.text.length <= 4096;
  }

  void _point(Offset offset, Size size, String button) {
    if (!controller.canInput || size.width <= 0 || size.height <= 0) return;
    unawaited(
      controller.operate('mouse', {
        'x': math.max(
          0,
          math.min(1023, (offset.dx / size.width * 1024).floor()),
        ),
        'y': math.max(
          0,
          math.min(767, (offset.dy / size.height * 768).floor()),
        ),
        'button': button,
      }),
    );
  }

  @override
  Widget build(BuildContext context) {
    final browser = ref.watch(privateBrowserControllerProvider(widget.scope));
    final i18n = ref.watch(i18nProvider);
    String t(String key) => i18n.t('workbench:privateBrowser.$key');
    final row = browser.resource, frame = browser.frame;
    final disabled = !browser.canInput;
    final held =
        row?.status == 'hold' ||
        (row?.fence.ownerKind == 'human' &&
            row?.expiresAt != null &&
            !row!.expiresAt!.isAfter(DateTime.now()));

    Widget button(String label, VoidCallback? action, {String? key}) =>
        OutlinedButton(
          key: key == null ? null : ValueKey(key),
          onPressed: action,
          child: Text(t(label)),
        );

    return SafeArea(
      top: false,
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Text(t('hint')),
          if (!browser.loaded && !browser.error)
            const Center(child: CircularProgressIndicator()),
          if (browser.error)
            Text(t('unavailable'), key: const ValueKey('browser-error')),
          if (browser.unconfirmed)
            Text(
              i18n.t('private-browser:unknown'),
              key: const ValueKey('browser-unknown'),
            ),
          if (browser.pending != null) Text(t('draining')),
          if (browser.givenBack != null) ...[
            Text(t('givenBack'), key: const ValueKey('browser-given-back')),
            if (browser.givenBack!.resumed > 0)
              Text(
                i18n.t(
                  'workbench:privateBrowser.resumeRequested',
                  vars: {'count': browser.givenBack!.resumed},
                ),
              ),
            if (browser.givenBack!.changed > 0)
              Text(
                i18n.t(
                  'workbench:privateBrowser.taskChanged',
                  vars: {'count': browser.givenBack!.changed},
                ),
              ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 4,
            children: [
              if (browser.canPrepare)
                button(
                  'prepare',
                  browser.busy ? null : () => unawaited(browser.ensure()),
                ),
              if (row != null && browser.pending == null) ...[
                button(
                  'takeover',
                  browser.busy || !row.canTakeover
                      ? null
                      : () => unawaited(browser.control('takeover')),
                ),
                button(
                  'giveback',
                  browser.busy || !row.canGiveback
                      ? null
                      : () => unawaited(browser.control('giveback')),
                ),
                // Closing admission remains reachable while an operation is pending.
                button(
                  'stop',
                  browser.controlling
                      ? null
                      : () => unawaited(browser.control('close')),
                ),
              ],
              if (browser.pending != null)
                button(
                  'continue',
                  browser.busy
                      ? null
                      : () => unawaited(
                          browser.control(
                            browser.pending!.action,
                            continueOriginal: true,
                          ),
                        ),
                ),
              button(
                'check',
                browser.busy ? null : () => unawaited(browser.refresh()),
              ),
              if (browser.busy)
                const Padding(
                  padding: EdgeInsets.all(12),
                  child: SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                ),
            ],
          ),
          if (row != null && browser.pending == null)
            Text(
              held && !browser.controlled
                  ? i18n.t('private-browser:hold')
                  : t(browser.controlled ? 'controlled' : 'viewAfterTakeover'),
            ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: TextField(
                  key: const ValueKey('browser-address'),
                  controller: _url,
                  decoration: InputDecoration(labelText: t('address')),
                  keyboardType: TextInputType.url,
                  maxLength: 4096,
                  enabled: !disabled,
                  onChanged: (_) => setState(() {}),
                  onSubmitted: (_) {
                    if (!disabled && _validUrl) {
                      unawaited(
                        browser.operate('navigate', {'url': _url.text}),
                      );
                    }
                  },
                ),
              ),
              const SizedBox(width: 8),
              button(
                'go',
                disabled || !_validUrl
                    ? null
                    : () => unawaited(
                        browser.operate('navigate', {'url': _url.text}),
                      ),
              ),
            ],
          ),
          Wrap(
            spacing: 8,
            children: [
              for (final kind in ['back', 'reload'])
                button(
                  kind,
                  disabled ? null : () => unawaited(browser.operate(kind)),
                ),
              button(
                'capture',
                !browser.controlled || browser.busy || browser.unconfirmed
                    ? null
                    : () => unawaited(browser.operate('capture')),
              ),
            ],
          ),
          const SizedBox(height: 8),
          AspectRatio(
            aspectRatio: 4 / 3,
            child: frame == null
                ? DecoratedBox(
                    decoration: BoxDecoration(
                      border: Border.all(color: context.tokens.hair),
                    ),
                    child: Center(child: Text(t('noFrame'))),
                  )
                : LayoutBuilder(
                    builder: (context, constraints) {
                      final size = Size(
                        constraints.maxWidth,
                        constraints.maxHeight,
                      );
                      return Semantics(
                        label: t('clickFrame'),
                        button: true,
                        enabled: !disabled,
                        child: GestureDetector(
                          key: const ValueKey('browser-frame'),
                          onTapUp: disabled
                              ? null
                              : (details) => _point(
                                  details.localPosition,
                                  size,
                                  _mouseButton,
                                ),
                          onLongPressStart: disabled
                              ? null
                              : (details) => _point(
                                  details.localPosition,
                                  size,
                                  'right',
                                ),
                          child: Image.memory(
                            frame.bytes,
                            fit: BoxFit.fill,
                            gaplessPlayback: false,
                            excludeFromSemantics: true,
                            errorBuilder: (_, _, _) {
                              WidgetsBinding.instance.addPostFrameCallback((_) {
                                if (mounted) controller.rejectFrame(frame);
                              });
                              return Center(child: Text(t('noFrame')));
                            },
                          ),
                        ),
                      );
                    },
                  ),
          ),
          if (frame != null)
            Text(frame.url, maxLines: 2, overflow: TextOverflow.ellipsis),
          const SizedBox(height: 8),
          DropdownButtonFormField<String>(
            key: const ValueKey('browser-mouse'),
            initialValue: _mouseButton,
            decoration: InputDecoration(
              labelText: i18n.t('private-browser:mouseButton'),
            ),
            items: [
              for (final value in ['left', 'right', 'middle'])
                DropdownMenuItem(
                  value: value,
                  child: Text(i18n.t('private-browser:mouse.$value')),
                ),
            ],
            onChanged: disabled
                ? null
                : (value) => setState(() => _mouseButton = value!),
          ),
          Wrap(
            spacing: 8,
            children: [
              for (final item in [
                ('scrollUp', 0, -480),
                ('scrollDown', 0, 480),
                ('scrollLeft', -480, 0),
                ('scrollRight', 480, 0),
              ])
                OutlinedButton(
                  onPressed: disabled
                      ? null
                      : () => unawaited(
                          browser.operate('wheel', {
                            'x': 512,
                            'y': 384,
                            'delta_x': item.$2,
                            'delta_y': item.$3,
                          }),
                        ),
                  child: Text(
                    item.$2 == 0
                        ? t(item.$1)
                        : i18n.t('private-browser:${item.$1}'),
                  ),
                ),
            ],
          ),
          Row(
            children: [
              Expanded(
                child: TextField(
                  key: const ValueKey('browser-text'),
                  controller: _text,
                  decoration: InputDecoration(labelText: t('text')),
                  maxLength: 4096,
                  enabled: !disabled,
                  onChanged: (_) => setState(() {}),
                ),
              ),
              const SizedBox(width: 8),
              button(
                'send',
                disabled || _text.text.isEmpty
                    ? null
                    : () {
                        final value = _text.text;
                        _text.clear();
                        unawaited(browser.operate('text', {'text': value}));
                      },
              ),
            ],
          ),
          Row(
            children: [
              Expanded(
                child: DropdownButtonFormField<String>(
                  key: const ValueKey('browser-key'),
                  initialValue: _key,
                  decoration: InputDecoration(labelText: t('key')),
                  items: [
                    for (final value in privateBrowserKeys)
                      DropdownMenuItem(value: value, child: Text(value)),
                  ],
                  onChanged: disabled
                      ? null
                      : (value) => setState(() => _key = value!),
                ),
              ),
              const SizedBox(width: 8),
              button(
                'press',
                disabled
                    ? null
                    : () => unawaited(browser.operate('key', {'key': _key})),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
