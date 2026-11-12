import '../../../shared/models/interaction.dart';
import '../../../shared/models/json.dart';

/// A server-driven card (backend `question/cards.py`): an ordinary question
/// whose `detail` names its `kind`, how to `present` it, and one entry per
/// option with a stable `id`, a `role` and display extras.
///
/// The answer is still the option label, so an App that does not know the
/// kind answers it through the plain question card.
class ServerCard {
  const ServerCard({
    required this.request,
    required this.kind,
    required this.present,
    required this.header,
    required this.question,
    required this.options,
    required this.detail,
  });

  /// The card [request] carries, or null when it is not a server card.
  static ServerCard? of(QuestionRequest request) {
    if (request.questions.length != 1) return null;
    final item = request.questions.single;
    final detail = item.detail;
    final kind = asString(detail?['kind']);
    final present = asString(detail?['present']);
    if (detail == null || kind == null || present == null) return null;
    final extras = asList(
      detail['options'],
    ).whereType<Map<String, dynamic>>().toList();
    if (extras.length != item.options.length || item.options.length < 2) {
      return null;
    }
    return ServerCard(
      request: request,
      kind: kind,
      present: present,
      header: item.header ?? '',
      question: item.question,
      detail: detail,
      options: [
        for (var i = 0; i < item.options.length; i++)
          ServerCardOption(
            id: asString(extras[i]['id']) ?? '$i',
            label: item.options[i].label,
            description: item.options[i].description ?? '',
            cancel: asString(extras[i]['role']) == 'cancel',
            recommended: asBool(extras[i]['recommended']) ?? false,
            extra: extras[i],
          ),
      ],
    );
  }

  final QuestionRequest request;
  final String kind;

  /// `sheet` pops up on its own; `inline` stays in the transcript.
  final String present;
  final String header;
  final String question;
  final List<ServerCardOption> options;
  final Map<String, dynamic> detail;

  bool get popsUp => present == 'sheet';
}

class ServerCardOption {
  const ServerCardOption({
    required this.id,
    required this.label,
    required this.description,
    required this.cancel,
    required this.recommended,
    required this.extra,
  });

  final String id;
  final String label;
  final String description;

  /// "Keep things as they are": shown as the quieter choice.
  final bool cancel;
  final bool recommended;
  final Map<String, dynamic> extra;
}
