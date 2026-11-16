import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/i18n/i18n.dart';
import '../../api/assistant_api.dart';
import '../../state/chat_session_controller.dart';
import '../../state/config_providers.dart';
import 'voice_message_input.dart';

/// The entry supplies the destination; recording never redirects to the assistant.
Widget? conversationVoiceInput(
  WidgetRef ref, {
  required String sessionKey,
  bool disabled = false,
  Future<String> Function()? prepareSession,
  ValueChanged<String>? onSent,
  ValueChanged<String>? onTranscript,
}) {
  final scope = ref.watch(assistantScopeProvider);
  if (scope == null ||
      ref.watch(appConfigProvider).valueOrNull?.voiceInputEnabled != true) {
    return null;
  }
  return VoiceMessageInput(
    key: ValueKey('voice:${scope.userId}:${scope.workspaceId}:$sessionKey'),
    scope: scope,
    sessionId: sessionKey,
    conversation: true,
    disabled: disabled,
    prepareSession: prepareSession,
    onTranscript: onTranscript,
    onSend: (sessionId, text, clientId, voice) => ref
        .read(chatSessionProvider(sessionId).notifier)
        .send(text, clientId: clientId, voiceMessage: voice),
    onSent: onSent,
  );
}

/// A question answer remains a draft until the person confirms its card.
class VoiceAnswerInput extends ConsumerStatefulWidget {
  const VoiceAnswerInput({
    super.key,
    required this.draftKey,
    required this.onText,
    this.disabled = false,
  });
  final String draftKey;
  final ValueChanged<String> onText;
  final bool disabled;
  @override
  ConsumerState<VoiceAnswerInput> createState() => _VoiceAnswerInputState();
}

class _VoiceAnswerInputState extends ConsumerState<VoiceAnswerInput> {
  bool _open = false;
  @override
  Widget build(BuildContext context) {
    final input = conversationVoiceInput(
      ref,
      sessionKey: widget.draftKey,
      disabled: widget.disabled,
      onTranscript: (text) {
        widget.onText(text);
        setState(() => _open = false);
      },
    );
    if (input == null) return const SizedBox.shrink();
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            onPressed: widget.disabled
                ? null
                : () => setState(() => _open = !_open),
            icon: Icon(_open ? Icons.keyboard_outlined : Icons.mic_none),
            label: Text(
              i18n.t(
                _open
                    ? 'chat:voiceMessage.keyboard'
                    : 'chat:voiceMessage.answer',
              ),
            ),
          ),
        ),
        if (_open) input,
      ],
    );
  }
}
