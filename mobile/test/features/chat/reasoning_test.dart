// Port of web `features/chat/hooks/useReasoningChoice.test.ts` — the pure
// half of that hook lives in `utils/reasoning.dart` here, so the pick map is
// passed in rather than held in a widget.
import 'package:bossip_mobile/features/chat/utils/reasoning.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:flutter_test/flutter_test.dart';

const _gpt = ModelInfo(
  id: 'openai/gpt-5.4',
  name: 'GPT-5.4',
  variants: ['none', 'low', 'medium', 'high', 'xhigh'],
  defaultVariant: 'none',
);
const _deepseek = ModelInfo(
  id: 'deepseek/deepseek-v4-flash',
  name: 'DeepSeek V4 Flash',
  variants: ['off', 'low', 'high', 'max'],
  defaultVariant: 'high',
);
const _plain = ModelInfo(id: 'plain', name: 'Plain');

void main() {
  test('restores a persisted strength only for the model that owns it', () {
    final owned = resolveReasoning(
      model: _gpt,
      sessionModel: _gpt.id,
      sessionVariant: 'xhigh',
    );
    expect(owned.activeId, 'xhigh');
    expect(owned.value, isNull);

    final other = resolveReasoning(
      model: _deepseek,
      sessionModel: _gpt.id,
      sessionVariant: 'xhigh',
    );
    expect(other.activeId, isNull);
    expect(other.value, Variant.modelDefault);
  });

  test('keeps picks independent per session and model', () {
    final picks = <String, Variant>{
      reasoningKey('s1', _gpt.id): const Variant('high'),
      reasoningKey('s1', _deepseek.id): const Variant('max'),
    };

    final gpt = resolveReasoning(
      model: _gpt,
      pick: picks[reasoningKey('s1', _gpt.id)],
    );
    expect(gpt.activeId, 'high');
    expect(gpt.value, const Variant('high'));

    final deepseek = resolveReasoning(
      model: _deepseek,
      pick: picks[reasoningKey('s1', _deepseek.id)],
    );
    expect(deepseek.activeId, 'max');
    expect(deepseek.value, const Variant('max'));

    // Another conversation has not picked anything for the same model.
    expect(picks[reasoningKey('s2', _gpt.id)], isNull);
  });

  test('an explicit default is a request, not an omission', () {
    final choice = resolveReasoning(
      model: _gpt,
      sessionModel: _gpt.id,
      sessionVariant: 'high',
      pick: Variant.modelDefault,
    );
    expect(choice.activeId, isNull);
    expect(choice.defaultId, 'none');
    expect(choice.value, Variant.modelDefault);
  });

  test('offers no control for a model without declared variants', () {
    final choice = resolveReasoning(model: _plain);
    expect(choice.variants, isEmpty);
    expect(choice.activeId, isNull);
    expect(choice.value, isNull);
  });

  test('clears a persisted effort when moving to a model with no selector',
      () {
    final choice = resolveReasoning(
      model: _plain,
      sessionModel: _gpt.id,
      sessionVariant: 'high',
    );
    expect(choice.activeId, isNull);
    expect(choice.value, Variant.modelDefault);
  });

  test('normalizes a strength the current model no longer advertises', () {
    final choice = resolveReasoning(
      model: _deepseek,
      sessionModel: _deepseek.id,
      sessionVariant: 'medium',
    );
    expect(choice.activeId, isNull);
    expect(choice.value, Variant.modelDefault);
  });

  test('the active model is the pick, then the session, then the default', () {
    expect(
      activeModelId(picked: 'a', sessionModel: 'b', defaultModel: 'c'),
      'a',
    );
    expect(activeModelId(picked: '', sessionModel: 'b', defaultModel: 'c'), 'b');
    expect(activeModelId(sessionModel: '', defaultModel: 'c'), 'c');
    expect(activeModelId(), '');
  });
}
