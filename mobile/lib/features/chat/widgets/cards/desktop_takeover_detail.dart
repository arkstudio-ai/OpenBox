import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/events/bus.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';

/// What a `desktop_takeover` question carries (web `DesktopTakeoverDetail`).
class TakeoverDetail {
  const TakeoverDetail({
    required this.reason,
    required this.url,
    required this.host,
    required this.browser,
  });

  final String reason;
  final String url;
  final String host;

  /// `local` = the page is on the cloud desktop, so we can jump there;
  /// `extension` = it is in the user's own browser.
  final String browser;

  bool get onDesktop => browser == 'local';
}

const _reasons = {
  'captcha_slider',
  'captcha_click',
  'captcha_math',
  'sms_code',
  'login_required',
  'risk_control',
  'other',
};

String _text(Map<String, dynamic> record, String key) {
  final value = record[key];
  return value is String ? value : '';
}

/// The detail of a desktop_takeover question, or null for anything else.
TakeoverDetail? readTakeoverDetail(Map<String, dynamic>? detail) {
  if (detail == null || detail['kind'] != 'desktop_takeover') return null;
  final reason = _text(detail, 'reason');
  return TakeoverDetail(
    reason: reason.isEmpty ? 'other' : reason,
    url: _text(detail, 'url'),
    host: _text(detail, 'host'),
    browser: detail['browser'] == 'extension' ? 'extension' : 'local',
  );
}

/// i18n key for a reason; unknown reasons fall back to the generic label.
String takeoverReasonKey(String reason) =>
    'chat:takeover.reason.${_reasons.contains(reason) ? reason : 'other'}';

/// The takeover card under a `desktop_takeover` question: what blocked the
/// agent, where, and — when the page is on the cloud desktop — one tap that
/// opens the desktop surface with input control already on. The question
/// text already says what to do, so it is not repeated here.
class DesktopTakeoverDetail extends ConsumerWidget {
  const DesktopTakeoverDetail({
    super.key,
    required this.item,
    required this.sessionId,
  });

  final QuestionItem item;
  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = readTakeoverDetail(item.detail);
    if (detail == null) return const SizedBox.shrink();

    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.all(11),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
        color: t.bg,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 6,
            runSpacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Text(
                i18n.t('chat:takeover.reasonLabel'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
              _Pill(
                i18n.t(takeoverReasonKey(detail.reason)),
                background: t.a100,
                foreground: t.a800,
              ),
              if (detail.host.isNotEmpty) ...[
                Text(
                  i18n.t('chat:takeover.site'),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
                _Pill(detail.host, background: t.hairSoft, foreground: t.n700),
              ],
            ],
          ),
          const SizedBox(height: 10),
          if (detail.onDesktop) ...[
            FilledButton.icon(
              onPressed: () => ref.read(appEventBusProvider).emit(
                'workbench.open',
                {'kind': 'desktop', 'sessionId': sessionId, 'control': true},
              ),
              style: FilledButton.styleFrom(
                backgroundColor: t.ink,
                foregroundColor: t.bg,
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
              ),
              icon: const Icon(Icons.desktop_windows_outlined, size: 16),
              label: Text(
                i18n.t('chat:takeover.open'),
                style: const TextStyle(fontSize: FontSizes.sm),
              ),
            ),
            const SizedBox(height: 6),
            Text(
              i18n.t('chat:takeover.openHint'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          ] else
            Text(
              i18n.t('chat:takeover.ownBrowser'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
            ),
        ],
      ),
    );
  }
}

class _Pill extends StatelessWidget {
  const _Pill(this.text, {required this.background, required this.foreground});

  final String text;
  final Color background;
  final Color foreground;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Text(text, style: TextStyle(fontSize: FontSizes.xs, color: foreground)),
    );
  }
}
