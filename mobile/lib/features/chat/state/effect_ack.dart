import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Execution pages whose last send was refused with
/// `ASSISTANT_EFFECT_UNRESOLVED` (web `effect-acknowledge` store, session
/// half): the page shows "我已核对，继续" above the composer even before the
/// session itself reports `effect_unknown`.
final executionEffectAckRequestedProvider =
    StateProvider.family<bool, String>((ref, sessionId) => false);
