import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Something the app layer adds to every task card, by task id (the call's
/// 盯现场 switch, VOICE_SEAM_PLAN §13.1). Features do not import each other:
/// `chat` draws the card and asks here, `app` supplies the voice widget. The
/// default supplies nothing.
typedef TaskCardExtraBuilder = Widget Function(String taskId);

final taskCardExtraProvider = Provider<TaskCardExtraBuilder?>((_) => null);
