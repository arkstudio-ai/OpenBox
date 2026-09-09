import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/shared/models/message.dart';

typedef VideoTranscript = List<Map<String, dynamic>>;

VideoTranscript directVideoJson() =>
    (jsonDecode(
              File(
                '../frontend-v2/src/features/chat/lib/__fixtures__/direct-video-messages.json',
              ).readAsStringSync(),
            )
            as List<dynamic>)
        .cast<Map<String, dynamic>>();

List<ChatMessage> directVideoMessages([VideoTranscript? data]) =>
    (data ?? directVideoJson()).map(ChatMessage.fromJson).toList();
