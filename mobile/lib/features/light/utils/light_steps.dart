import '../../../shared/models/interaction.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/light.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';

/// The step a question card belongs to: `detail.light.step_id`, which the
/// agent writes on every card of a topic chat (docs `LIGHT_MODE_BACKEND.md`).
/// The last question of a multi-question card wins.
String? lightStepIdOf(QuestionRequest request) {
  for (final question in request.questions.reversed) {
    final id = asString(asMap(question.detail?['light'])['step_id']);
    if (id != null && id.isNotEmpty) return id;
  }
  return null;
}

/// 0-based position of [stepId] in [steps]; -1 when unknown or absent.
int lightStepIndex(List<LightStep> steps, String? stepId) {
  if (stepId == null) return -1;
  for (final (index, step) in steps.indexed) {
    if (step.id == stepId) return index;
  }
  return -1;
}

/// True once the conversation holds a finished deliverable — a final video or
/// a result image. The last step (保存发布 / 出图) has no card of its own: the
/// result card is that step, so the bar lights it when the result lands.
bool lightResultDelivered(List<ChatMessage> messages) {
  for (final message in messages.reversed) {
    for (final part in message.parts) {
      if (part is! FilePart) continue;
      final role = part.relation?.role;
      final mime = part.mimeType ?? '';
      if ((role == 'final' || role == 'result') &&
          (mime.startsWith('video/') || mime.startsWith('image/')) &&
          message.role == 'assistant') {
        return true;
      }
    }
  }
  return false;
}
