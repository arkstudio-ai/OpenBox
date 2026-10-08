import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../i18n/i18n.dart';
import '../models/json.dart';
import '../ws/ws_client.dart';
import 'providers.dart';

/// What the first meeting asks, in order (web `INTRO_STEPS`).
const introStepNames = ['address', 'name', 'length', 'business'];

/// Kinds of business with starter ideas; anything else is the person's own
/// words.
const businessKinds = ['beauty', 'food', 'retail'];

/// Where the person's first meeting with the assistant got to (backend
/// `assistant/profile.py` `intro_event`).
class IntroProgress {
  const IntroProgress({
    this.status = 'new',
    this.steps = const {},
    this.nudged = false,
  });

  static const statuses = ['new', 'started', 'done', 'dismissed', 'bypassed'];

  /// new → started → done, or dismissed ("以后再说"), or bypassed (they went
  /// straight to work).
  final String status;

  /// Each question answered or skipped in the meeting.
  final Map<String, String> steps;

  /// The one reminder after going straight to work was shown.
  final bool nudged;

  factory IntroProgress.fromJson(Object? raw) {
    final value = asMap(raw);
    final status = asString(value['status']);
    return IntroProgress(
      status: statuses.contains(status) ? status! : 'new',
      steps: {
        for (final entry in asMap(value['steps']).entries)
          if (introStepNames.contains(entry.key) &&
              (entry.value == 'answered' || entry.value == 'skipped'))
            entry.key: entry.value as String,
      },
      nudged: value['nudged'] == true,
    );
  }
}

/// How the person wants their assistant (web `assistant-profile.ts`, backend
/// `assistant/profile.py`): what it is called, what it calls them, how it
/// talks, their business and how calls go. One preference for both apps, the
/// assistant's own replies and the phone front desk.
///
/// Set or not is a decision, not a value: [decided] names the fields the
/// person chose (choosing the default counts; skipping a question does not),
/// and [intro] is where their first meeting got to.
class AssistantProfile {
  const AssistantProfile({
    this.name = '',
    this.address = '',
    this.tone = 'warm',
    this.length = 'balanced',
    this.emoji = false,
    this.persona = '',
    this.callRecap = true,
    this.callReports = true,
    this.callDetail = 'brief',
    this.business = '',
    this.decided = const {},
    this.intro = const IntroProgress(),
  });

  static const tones = ['warm', 'professional', 'lively'];
  static const lengths = ['brief', 'balanced', 'detailed'];
  static const callDetails = ['brief', 'detailed'];

  /// "" is the default name the UI translates (个人助理 / Personal assistant).
  final String name;

  /// What it calls the person; "" for no name.
  final String address;
  final String tone;
  final String length;
  final bool emoji;

  /// The person's own words on how it should be, up to 300 characters.
  final String persona;

  /// A call opens by mentioning the last one.
  final bool callRecap;

  /// A call tells finished work nobody asked about on it.
  final bool callReports;
  final String callDetail;

  /// beauty / food / retail, or their own words; "" not told.
  final String business;

  /// The fields the person decided (settings, chat or the intro).
  final Set<String> decided;
  final IntroProgress intro;

  /// Today's value of a first-meeting question's field.
  String valueOf(String step) => switch (step) {
    'address' => address,
    'name' => name,
    'length' => length,
    'business' => business,
    _ => '',
  };

  /// What still holds is kept, the rest is the default. The first version
  /// kept only the name.
  factory AssistantProfile.fromJson(Object? raw) {
    final value = asMap(raw);
    String oneOf(List<String> choices, Object? v, String fallback) =>
        v is String && choices.contains(v) ? v : fallback;
    const d = AssistantProfile();
    return AssistantProfile(
      name: asString(value['name']) ?? '',
      address: asString(value['address']) ?? '',
      tone: oneOf(tones, value['tone'], d.tone),
      length: oneOf(lengths, value['length'], d.length),
      emoji: asBool(value['emoji']) ?? d.emoji,
      persona: asString(value['persona']) ?? '',
      callRecap: asBool(value['call_recap']) ?? d.callRecap,
      callReports: asBool(value['call_reports']) ?? d.callReports,
      callDetail: oneOf(callDetails, value['call_detail'], d.callDetail),
      business: asString(value['business']) ?? '',
      decided: {
        for (final key in asMap(value['decided']).keys)
          if (asMap(asMap(value['decided'])[key]).isNotEmpty) key,
      },
      intro: IntroProgress.fromJson(value['intro']),
    );
  }

  Map<String, Object> toJson() => {
    'name': name,
    'address': address,
    'tone': tone,
    'length': length,
    'emoji': emoji,
    'persona': persona,
    'call_recap': callRecap,
    'call_reports': callReports,
    'call_detail': callDetail,
    'business': business,
  };
}

/// Which questions a meeting asks. `auto` (the first one, on the welcome
/// page): neither decided nor answered or skipped yet. `undecided` (opened by
/// the person later): every one still undecided. `all` ("重新认识一下" in
/// Settings): every question, today's values as the defaults.
List<String> introStepsFor(AssistantProfile profile, String mode) {
  if (mode == 'all') return [...introStepNames];
  return [
    for (final step in introStepNames)
      if (!profile.decided.contains(step) &&
          (mode == 'undecided' || !profile.intro.steps.containsKey(step)))
        step,
  ];
}

/// The first meeting shows by itself on the welcome page: not ended, and
/// something still to ask.
bool introStartsByItself(AssistantProfile profile) =>
    (profile.intro.status == 'new' || profile.intro.status == 'started') &&
    introStepsFor(profile, 'auto').isNotEmpty;

/// One thing the assistant learned about how the person likes to be helped;
/// removable like any memory.
class LearnedItem {
  const LearnedItem({
    required this.id,
    required this.revision,
    required this.summary,
  });
  final String id;
  final int revision;
  final String summary;
}

class LearnedStyle {
  const LearnedStyle({this.learned = const [], this.reactions = const []});

  final List<LearnedItem> learned;

  /// (reason, times) picked with thumbs-downs in the assistant's chat over the
  /// last 30 days.
  final List<(String, int)> reactions;

  factory LearnedStyle.fromJson(Map<String, dynamic> data) => LearnedStyle(
    learned: [
      for (final item in asList(data['learned']))
        if (asString(asMap(item)['id']) != null)
          LearnedItem(
            id: asString(asMap(item)['id'])!,
            revision: asInt(asMap(item)['revision']) ?? 0,
            summary: asString(asMap(item)['summary']) ?? '',
          ),
    ],
    reactions: [
      for (final item in asList(data['reactions']))
        if (asString(asMap(item)['reason']) != null)
          (asString(asMap(item)['reason'])!, asInt(asMap(item)['count']) ?? 0),
    ],
  );
}

/// `GET/PUT /api/assistant/profile`, and what it learned on its own.
class AssistantProfileApi {
  AssistantProfileApi(this._dio);

  final Dio _dio;

  Future<AssistantProfile> get() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/assistant/profile');
    return AssistantProfile.fromJson(resp.data);
  }

  /// Saves the fields given (the server cleans names to one line of up to 20
  /// characters) and returns the whole profile it kept.
  Future<AssistantProfile> save(Map<String, Object> patch) async {
    final resp = await _dio.put<Map<String, dynamic>>(
      '/api/assistant/profile',
      data: patch,
    );
    return AssistantProfile.fromJson(resp.data);
  }

  /// Records a step of the first meeting (`answer`, `skip`, `dismiss`,
  /// `bypass`, `nudged`) and returns the whole profile after it.
  Future<AssistantProfile> intro(Map<String, Object> event) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/assistant/intro',
      data: event,
    );
    return AssistantProfile.fromJson(resp.data);
  }

  Future<LearnedStyle> learned() async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/assistant/profile/learned',
    );
    return LearnedStyle.fromJson(resp.data ?? const {});
  }

  /// Forget one learned item, as the knowledge page does: it stops being
  /// followed and is not learned again.
  Future<void> forget(LearnedItem item, {required String requestId}) async {
    await _dio.post<Map<String, dynamic>>(
      '/api/memories/${Uri.encodeComponent(item.id)}/forget',
      data: {
        'expected_revision': item.revision,
        'request_id': requestId,
        'mode': 'memory',
        'source_ids': const <String>[],
      },
    );
  }
}

final assistantProfileApiProvider = Provider<AssistantProfileApi>(
  (ref) => AssistantProfileApi(ref.watch(apiDioProvider)),
);

/// Pushed when the profile changes (Settings on any device, or the assistant
/// itself when told "以后叫你小七"); `__connected` reads it again in case one
/// was missed.
const assistantProfileEvent = 'assistant.profile.updated';

class AssistantProfileNotifier extends AsyncNotifier<AssistantProfile> {
  @override
  Future<AssistantProfile> build() {
    try {
      final sub = ref.watch(wsClientProvider).events.listen((event) {
        if (event.type == assistantProfileEvent) {
          state = AsyncData(AssistantProfile.fromJson(event.data['profile']));
        } else if (event.type == '__connected') {
          ref.invalidateSelf();
        }
      });
      ref.onDispose(sub.cancel);
    } catch (_) {
      // No socket here (early startup, a test): the profile still loads; it
      // just does not follow a change made elsewhere until the next read.
    }
    return ref.watch(assistantProfileApiProvider).get();
  }

  Future<AssistantProfile> save(Map<String, Object> patch) async {
    final kept = await ref.read(assistantProfileApiProvider).save(patch);
    state = AsyncData(kept);
    return kept;
  }

  /// Records a step of the first meeting; an answer is saved as the person's
  /// decision.
  Future<void> recordIntro(Map<String, Object> event) async {
    state = AsyncData(await ref.read(assistantProfileApiProvider).intro(event));
  }
}

final assistantProfileProvider =
    AsyncNotifierProvider<AssistantProfileNotifier, AssistantProfile>(
      AssistantProfileNotifier.new,
    );

final learnedStyleProvider = FutureProvider.autoDispose<LearnedStyle>(
  (ref) => ref.watch(assistantProfileApiProvider).learned(),
);

String _customName(WidgetRef ref) =>
    ref.watch(assistantProfileProvider).valueOrNull?.name ?? '';

/// The assistant's name where it is a label or a heading: the chosen name,
/// else the translated default (web `useAssistantNames().title`).
String assistantLabel(WidgetRef ref) {
  final name = _customName(ref);
  return name.isNotEmpty
      ? name
      : ref.watch(i18nProvider).t('common:assistantName.title');
}

/// The assistant's name inside a sentence ("和{{name}}通话").
String assistantMention(WidgetRef ref) {
  final name = _customName(ref);
  return name.isNotEmpty
      ? name
      : ref.watch(i18nProvider).t('common:assistantName.mention');
}
