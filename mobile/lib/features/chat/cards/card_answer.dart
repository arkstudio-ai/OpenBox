import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/events/bus.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/toast.dart';
import '../api/assistant_reply.dart';
import '../api/chat_api.dart';
import '../state/pending_store.dart';
import 'server_card.dart';

/// Answers a server card with one option, or dismisses it ([option] null),
/// through the same routes the question card uses: the assistant's
/// versioned reply for a task linked to the assistant, else the plain
/// question reply. Returns true when the card is settled (answered, or
/// already gone) and may close.
Future<bool> answerServerCard(
  WidgetRef ref,
  ServerCard card,
  ServerCardOption? option,
) async {
  final request = card.request;
  // Read everything before awaiting: the host may rebuild meanwhile.
  final api = ref.read(chatApiProvider);
  final pending = ref.read(pendingProvider.notifier);
  final toast = ref.read(toastProvider.notifier);
  final i18n = ref.read(i18nProvider);
  final events = ref.read(appEventBusProvider);
  final service = request.assistant == null
      ? null
      : ref.read(assistantReplyProvider);
  final answers = option == null
      ? null
      : [
          [option.label],
        ];
  try {
    if (service != null) {
      final receipt = await service.reply(
        kind: 'question',
        id: request.id,
        binding: request.assistant!,
        answer: {'answers': ?answers},
        reject: answers == null,
      );
      toast.info(i18n.t(replyStateKey(receipt['state'])));
    } else if (answers == null) {
      await api.rejectQuestion(request.id);
    } else {
      await api.replyQuestion(request.id, answers);
    }
    pending.removeQuestion(request.id);
    events.emit('question.resolved', {'sessionId': request.sessionId});
    return true;
  } catch (error) {
    final code = apiErrorOf(error)?.status;
    if (code == 404 || code == 410) {
      pending.removeQuestion(request.id);
      toast.error(i18n.t('chat:question.gone'));
      await pending.refreshQuestions();
      return true;
    }
    toast.error(
      i18n.t(
        apiErrorOf(error)?.code == 'ASSISTANT_SEND_UNCERTAIN'
            ? 'chat:assistant.requests.replyUncertain'
            : code == 409
            ? 'chat:question.conflict'
            : 'chat:question.submitFailed',
      ),
    );
    if (code == 409) await pending.refreshQuestions();
    return false;
  }
}
