import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import 'assistant_avatar.dart';

/// Before dawn is still the evening for anyone awake to read this
/// (web `AssistantWelcome.timeOfDay`).
String welcomeTimeOfDay(DateTime now) {
  final hour = now.hour;
  if (hour >= 5 && hour < 12) return 'morning';
  if (hour >= 12 && hour < 18) return 'afternoon';
  return 'evening';
}

/// The first thing someone sees in the personal assistant: who it is, what
/// to hand it, and a few things to try (web `AssistantWelcome`). A card fills
/// the composer rather than sending, so a first-time user sees the words
/// before anything happens.
class AssistantWelcome extends ConsumerWidget {
  const AssistantWelcome({super.key, required this.onPick, this.clock});

  /// Puts an idea's prompt into the composer.
  final ValueChanged<String> onPick;

  /// The time the greeting is for; now unless a test pins it.
  final DateTime Function()? clock;

  static const ideas = <(String, IconData)>[
    ('delegate', Icons.assignment_outlined),
    ('progress', Icons.checklist_rounded),
    ('waiting', Icons.notifications_active_outlined),
    ('remember', Icons.psychology_outlined),
    ('briefing', Icons.wb_sunny_outlined),
    ('credits', Icons.toll_outlined),
  ];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final name = ref.watch(authProvider).user?.username.trim() ?? '';
    var greeting = i18n.t(
      'chat:assistant.welcome.greeting.${welcomeTimeOfDay((clock ?? DateTime.now)())}',
      vars: {'name': name},
    );
    // Without a name the greeting would end on a dangling comma.
    if (name.isEmpty) greeting = greeting.replaceFirst(RegExp(r'[,，\s]+$'), '');
    return SingleChildScrollView(
      keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
      padding: const EdgeInsets.fromLTRB(16, 32, 16, 16),
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 760),
          child: Column(
            children: [
              const AssistantAvatar(large: true),
              const SizedBox(height: 20),
              Semantics(
                header: true,
                child: Text(
                  greeting,
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: FontSizes.xl3,
                    height: 1.3,
                    fontWeight: FontWeight.w500,
                    color: t.ink,
                  ),
                ),
              ),
              const SizedBox(height: 12),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 520),
                child: Text(
                  i18n.t('chat:assistant.welcome.intro'),
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: FontSizes.base,
                    height: 1.65,
                    color: t.n700,
                  ),
                ),
              ),
              const SizedBox(height: 28),
              LayoutBuilder(
                builder: (context, constraints) {
                  // One column on a phone, two once there is room for both.
                  final columns = constraints.maxWidth >= 560 ? 2 : 1;
                  final width =
                      (constraints.maxWidth - (columns - 1) * 10) / columns;
                  return Wrap(
                    spacing: 10,
                    runSpacing: 10,
                    children: [
                      for (final (key, icon) in ideas)
                        SizedBox(
                          width: width,
                          child: _IdeaCard(
                            key: ValueKey('assistant-idea-$key'),
                            icon: icon,
                            title: i18n.t(
                              'chat:assistant.welcome.ideas.$key.title',
                            ),
                            prompt: i18n.t(
                              'chat:assistant.welcome.ideas.$key.prompt',
                            ),
                            onTap: () => onPick(
                              i18n.t(
                                'chat:assistant.welcome.ideas.$key.prompt',
                              ),
                            ),
                          ),
                        ),
                    ],
                  );
                },
              ),
              const SizedBox(height: 24),
              Text(
                i18n.t('chat:assistant.welcome.hint'),
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.6,
                  color: t.n600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _IdeaCard extends StatelessWidget {
  const _IdeaCard({
    super.key,
    required this.icon,
    required this.title,
    required this.prompt,
    required this.onTap,
  });

  final IconData icon;
  final String title;
  final String prompt;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Semantics(
      button: true,
      child: Material(
        color: t.card,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(Radii.xl),
          side: BorderSide(color: t.hair),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  width: 36,
                  height: 36,
                  decoration: BoxDecoration(
                    color: t.hairSoft,
                    borderRadius: BorderRadius.circular(12),
                  ),
                  alignment: Alignment.center,
                  child: Icon(icon, size: 18, color: t.n800),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        style: TextStyle(
                          fontSize: FontSizes.base,
                          fontWeight: FontWeight.w500,
                          color: t.ink,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        prompt,
                        style: TextStyle(
                          fontSize: FontSizes.sm,
                          height: 1.45,
                          color: t.n600,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
