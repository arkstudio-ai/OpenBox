import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/models/app_config.dart';
import '../../../shared/models/team.dart';
import '../api/chat_api.dart';

/// `GET /api/agent/config` — models + defaults (web `useConfigQuery`).
final appConfigProvider = FutureProvider<AppConfig>(
  (ref) => ref.watch(chatApiProvider).getConfig(),
);

/// `GET /api/agent/agent` — conversational agents (web `useChatAgents`).
final chatAgentsProvider = FutureProvider<List<AgentInfo>>(
  (ref) => ref.watch(chatApiProvider).listAgents(),
);

/// Unsent per-session agent pick (overlays `session.agent`); key `draft`
/// is the not-yet-created session on the empty screen.
final pickedAgentProvider = StateProvider.family<String?, String>(
  (ref, sessionId) => null,
);

final pickedTeamProvider = StateProvider.family<TeamRequest?, String>(
  (ref, sessionId) => null,
);
