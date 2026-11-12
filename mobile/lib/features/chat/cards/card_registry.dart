import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/models/json.dart';
import 'server_card.dart';

/// Per-kind extras for a server card. A kind without an entry still pops up
/// and answers: the sheet shows its question and option labels as they are.
/// Adding a card kind means registering it on the backend
/// (`question/cards.py`) and, only if it needs more than labels, here.
class CardRenderer {
  const CardRenderer({this.optionDetail});

  /// Extra line(s) under one option's label and description.
  final Widget? Function(BuildContext context, ServerCardOption option)?
  optionDetail;
}

final Map<String, CardRenderer> cardRenderers = {
  // backend tool/video_model_card.py
  'video_model_switch': CardRenderer(optionDetail: _videoOptionDetail),
};

Widget? _videoOptionDetail(BuildContext context, ServerCardOption option) {
  final video = option.extra['video'];
  if (video is! Map<String, dynamic>) return null;
  final description = asString(video['description']) ?? '';
  // The option's own description is the price; the tier says what it is for.
  if (description.isEmpty || description == option.description) return null;
  final t = context.tokens;
  return Text(
    description,
    style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
  );
}
