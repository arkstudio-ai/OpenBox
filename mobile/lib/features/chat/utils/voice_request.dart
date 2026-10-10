import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';

/// Display the server's contextual handover while preserving its human source.
String? voiceRequest(ChatMessage message) {
  if (message.role != 'user') return null;
  final parts = message.parts
      .whereType<TextPart>()
      .where((part) => !part.synthetic)
      .toList();
  if (parts.length != 1) return null;
  final part = parts.single;
  if (part.origin != 'human' ||
      part.originRef['entrypoint'] != 'assistant_voice') {
    return null;
  }
  final context = part.originRef['voice_context'];
  if (context is! Map) return null;
  final request = context['request'];
  if (request is! String || request.trim().isEmpty) return null;
  String normal(String value) => value.trim().replaceAll(RegExp(r'\s+'), ' ');
  return normal(request) == normal(part.text) ? null : request.trim();
}
