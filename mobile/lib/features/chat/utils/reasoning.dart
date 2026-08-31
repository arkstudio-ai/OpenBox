// The reasoning strength sent with the next prompt (web
// `features/chat/hooks/useReasoningChoice.ts`).
//
// Model variants are picked independently from the model itself: only
// variants the active model declares are selectable, and "default" clears the
// conversation override. Picks are kept per conversation *and* model so
// switching models cannot leak an unsupported effort into the next request.
library;

import '../../../shared/models/app_config.dart';

/// One request's reasoning field. Dart has no `undefined`, so the whole
/// object stands in for it: a null [Variant] omits the field — which keeps
/// whatever the conversation already stores — while a [Variant] carrying a
/// null [level] is the explicit null that returns the model to its own
/// advertised default.
class Variant {
  const Variant(this.level);

  /// Explicit null: back to this model's advertised default.
  static const modelDefault = Variant(null);

  final String? level;

  @override
  bool operator ==(Object other) => other is Variant && other.level == level;

  @override
  int get hashCode => level.hashCode;
}

/// What the picker shows and what the next request carries.
class ReasoningChoice {
  const ReasoningChoice({
    required this.variants,
    required this.defaultId,
    required this.activeId,
    required this.value,
  });

  /// Strengths the active model declares; empty hides the picker entirely.
  final List<String> variants;

  /// The model's own default, shown next to the "default" entry.
  final String? defaultId;

  /// The selected strength, or null for "default".
  final String? activeId;

  /// The wire value — see [Variant].
  final Variant? value;
}

/// The model a conversation is about to send with: an unsent pick wins, then
/// the conversation's own model, then the deployment default.
String activeModelId({
  String? picked,
  String? sessionModel,
  String? defaultModel,
}) {
  if (picked != null && picked.isNotEmpty) return picked;
  if (sessionModel != null && sessionModel.isNotEmpty) return sessionModel;
  return defaultModel ?? '';
}

/// Pick storage key: a conversation *and* a model, so switching either one
/// starts from that pair's own state.
String reasoningKey(String sessionKey, String modelId) =>
    '$sessionKey\u0000$modelId';

/// Pure resolution of [pick] (null = nothing picked in this session/model
/// pair) against the conversation's persisted selection.
ReasoningChoice resolveReasoning({
  required ModelInfo? model,
  String? sessionModel,
  String? sessionVariant,
  Variant? pick,
}) {
  final variants = model?.variants ?? const <String>[];
  final hasSessionModel = sessionModel != null && sessionModel.isNotEmpty;
  final persisted = model != null &&
          model.id == sessionModel &&
          sessionVariant != null &&
          variants.contains(sessionVariant)
      ? sessionVariant
      : null;
  final activeId = model == null || variants.isEmpty
      ? null
      : pick != null
          ? (pick.level != null && variants.contains(pick.level)
              ? pick.level
              : null)
          : persisted;

  final Variant? value;
  if (model == null) {
    value = null;
  } else if (pick != null) {
    value = Variant(activeId);
  } else if (hasSessionModel && model.id != sessionModel) {
    // A model switch must not carry an effort the new family may reject. The
    // explicit null tells the server to use this model's own default.
    value = Variant.modelDefault;
  } else if (model.id == sessionModel &&
      sessionVariant != null &&
      !variants.contains(sessionVariant)) {
    value = Variant.modelDefault;
  } else {
    // No local choice: omission preserves the session value (or lets a new
    // conversation resolve the deployment default) without pinning it.
    value = null;
  }

  return ReasoningChoice(
    variants: variants,
    defaultId: model?.defaultVariant,
    activeId: activeId,
    value: value,
  );
}
