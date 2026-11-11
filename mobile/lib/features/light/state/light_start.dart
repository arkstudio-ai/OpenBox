import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/light.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../api/light_api.dart';
import 'light_providers.dart';

/// Where a greyed-out entry sends the person to lift what blocks it (docs
/// `LIGHT_MODE_PLAN.md` §4.2: 免费档缺云电脑的入口显示为灰卡，写清楚缺什么并一键去开通).
/// A desktop comes with a paid plan, so without one the plans page is the
/// answer either way; with one, the desktop page activates it.
String? lightUnavailableTarget(
  LightAvailability availability, {
  required bool paidPlan,
}) => switch (availability) {
  LightAvailability.needsPlan => Paths.billing(),
  LightAvailability.needsDesktop => paidPlan ? Paths.desktop : Paths.billing(),
  LightAvailability.needsBinding => Paths.authCenter(),
  LightAvailability.ok || LightAvailability.comingSoon => null,
};

/// `light:home.<key>` for the one-line reason on a grey card.
String lightAvailabilityKey(LightAvailability availability) =>
    switch (availability) {
      LightAvailability.needsPlan => 'needsPlan',
      LightAvailability.needsDesktop => 'needsDesktop',
      LightAvailability.needsBinding => 'needsBinding',
      LightAvailability.comingSoon => 'comingSoon',
      LightAvailability.ok => 'ok',
    };

/// Start a topic chat for [entry] and open it. Returns true when the chat
/// opened; false when the server refused and the person was told (and, for a
/// lifted-later refusal, sent where it can be lifted).
Future<bool> startLightTopic(
  BuildContext context,
  WidgetRef ref,
  LightEntry entry, {
  String? subtype,
  String? sample,
  bool replace = false,
}) async {
  final toast = ref.read(toastProvider.notifier);
  final i18n = ref.read(i18nProvider);
  try {
    final result = await ref
        .read(lightApiProvider)
        .start(entryId: entry.id, subtype: subtype, sample: sample);
    if (result.sessionId.isEmpty) {
      throw StateError('The server returned no session');
    }
    ref
        .read(lightStartsProvider.notifier)
        .update((starts) => {...starts, result.sessionId: result});
    ref.invalidate(lightRecentProvider);
    ref.invalidate(lightConversationsProvider);
    if (!context.mounted) return false;
    final path = Paths.lightChat(result.sessionId);
    if (replace) {
      context.pushReplacement(path);
    } else {
      unawaited(context.push(path));
    }
    return true;
  } catch (error) {
    final failure = apiErrorOf(error);
    if (!context.mounted) return false;
    if (failure?.code == 'LIGHT_ENTRY_UNAVAILABLE') {
      final availability = lightAvailabilityFrom(
        failure!.meta['availability'] as String?,
      );
      toast.warning(
        i18n.t(
          'light:gallery.unavailable',
          vars: {
            'reason': i18n.t(
              'light:home.${lightAvailabilityKey(availability)}',
            ),
          },
        ),
      );
      ref.invalidate(lightEntriesProvider);
      final paid =
          ref.read(lightEntriesProvider).valueOrNull?.paidPlan ?? false;
      final target = lightUnavailableTarget(availability, paidPlan: paid);
      if (target != null) unawaited(context.push(target));
    } else {
      toast.error(errorText(i18n, error));
    }
    return false;
  }
}
