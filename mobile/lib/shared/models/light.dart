/// Light-mode contracts (backend `api/light.py`, `light/entries.json`; docs
/// `LIGHT_MODE_BACKEND.md`): the home entries with their availability, a
/// started topic chat, and the recent image/video results grouped by
/// conversation.
library;

import 'json.dart';

/// Why an entry is or is not usable right now, computed by the server for
/// this workspace. The app only renders it.
enum LightAvailability { ok, needsPlan, needsDesktop, needsBinding, comingSoon }

LightAvailability lightAvailabilityFrom(String? value) => switch (value) {
  'ok' => LightAvailability.ok,
  'needs_plan' => LightAvailability.needsPlan,
  'needs_desktop' => LightAvailability.needsDesktop,
  'needs_binding' => LightAvailability.needsBinding,
  _ => LightAvailability.comingSoon,
};

/// One step of the bar above a topic chat; a `question` card's
/// `detail.light.step_id` names the one it belongs to.
class LightStep {
  const LightStep({required this.id, required this.label});

  static LightStep? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final id = asString(json['id']);
    if (id == null || id.isEmpty) return null;
    return LightStep(id: id, label: asString(json['label']) ?? id);
  }

  final String id;
  final String label;
}

/// A sample in the gallery: the look, what it is for, what to prepare.
class LightSample {
  const LightSample({
    required this.id,
    required this.label,
    this.description = '',
    this.needs = '',
    this.mediaUrl,
  });

  static LightSample? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final id = asString(json['id']);
    if (id == null || id.isEmpty) return null;
    final media = asString(json['media_url']);
    return LightSample(
      id: id,
      label: asString(json['label']) ?? id,
      description: asString(json['description']) ?? '',
      needs: asString(json['needs']) ?? '',
      mediaUrl: media == null || media.isEmpty ? null : media,
    );
  }

  final String id;
  final String label;
  final String description;
  final String needs;

  /// Still empty on the server today; the gallery shows a placeholder tile.
  final String? mediaUrl;
}

class LightSubtype {
  const LightSubtype({
    required this.id,
    required this.label,
    this.samples = const [],
  });

  static LightSubtype? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final id = asString(json['id']);
    if (id == null || id.isEmpty) return null;
    return LightSubtype(
      id: id,
      label: asString(json['label']) ?? id,
      samples: asList(
        json['samples'],
      ).map(LightSample.fromJson).whereType<LightSample>().toList(),
    );
  }

  final String id;
  final String label;
  final List<LightSample> samples;
}

/// The first assistant message of a topic chat and the chips the input bar
/// offers for the first reply.
class LightOpeningCard {
  const LightOpeningCard({this.text = '', this.chips = const []});

  factory LightOpeningCard.fromJson(dynamic json) {
    final map = asMap(json);
    return LightOpeningCard(
      text: asString(map['text']) ?? '',
      chips: asList(map['chips']).whereType<String>().toList(),
    );
  }

  final String text;
  final List<String> chips;
}

class LightEntry {
  const LightEntry({
    required this.id,
    required this.title,
    this.subtitle = '',
    this.kind = 'primary',
    this.skill = '',
    this.defaultTier,
    this.priceHint = '',
    this.requiresPlan = false,
    this.requiresDesktop = false,
    this.requiresBinding,
    this.steps = const [],
    this.subtypes = const [],
    this.openingCard = const LightOpeningCard(),
    this.sampleMedia = const [],
    this.availability = LightAvailability.ok,
  });

  static LightEntry? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final id = asString(json['id']);
    if (id == null || id.isEmpty) return null;
    final requires = asMap(json['requires']);
    final binding = asString(requires['binding']);
    return LightEntry(
      id: id,
      title: asString(json['title']) ?? id,
      subtitle: asString(json['subtitle']) ?? '',
      kind: asString(json['kind']) ?? 'primary',
      skill: asString(json['skill']) ?? '',
      defaultTier: asString(json['default_tier']),
      priceHint: asString(json['price_hint']) ?? '',
      requiresPlan: asBool(requires['plan']) ?? requires['plan'] != null,
      requiresDesktop: asBool(requires['desktop']) ?? false,
      requiresBinding: binding == null || binding.isEmpty ? null : binding,
      steps: asList(
        json['steps'],
      ).map(LightStep.fromJson).whereType<LightStep>().toList(),
      subtypes: asList(
        json['subtypes'],
      ).map(LightSubtype.fromJson).whereType<LightSubtype>().toList(),
      openingCard: LightOpeningCard.fromJson(json['opening_card']),
      sampleMedia: [
        for (final item in asList(json['sample_media']))
          if (item is String && item.isNotEmpty)
            item
          else
            ?asString(asMap(item)['url']),
      ],
      availability: lightAvailabilityFrom(asString(json['availability'])),
    );
  }

  final String id;
  final String title;
  final String subtitle;

  /// `primary` (a big card), `secondary` (a small one) or `coming_soon`.
  final String kind;
  final String skill;
  final String? defaultTier;
  final String priceHint;
  final bool requiresPlan;
  final bool requiresDesktop;
  final String? requiresBinding;
  final List<LightStep> steps;
  final List<LightSubtype> subtypes;
  final LightOpeningCard openingCard;
  final List<String> sampleMedia;
  final LightAvailability availability;

  bool get isPrimary => kind == 'primary';
  bool get isSecondary => kind == 'secondary';
  bool get isComingSoon =>
      kind == 'coming_soon' || availability == LightAvailability.comingSoon;
  bool get available => availability == LightAvailability.ok;

  /// Whether there is a gallery to pick a look from before the chat starts.
  bool get hasGallery => subtypes.any((s) => s.samples.isNotEmpty);

  LightStep? stepById(String? id) {
    if (id == null) return null;
    for (final step in steps) {
      if (step.id == id) return step;
    }
    return null;
  }
}

/// `GET /api/light/entries`.
class LightEntriesView {
  const LightEntriesView({
    this.version = '',
    this.uiMode = 'pro',
    this.planId = 'free',
    this.desktop = false,
    this.entries = const [],
  });

  factory LightEntriesView.fromJson(Map<String, dynamic> json) =>
      LightEntriesView(
        version: asString(json['version']) ?? '',
        uiMode: asString(json['ui_mode']) ?? 'pro',
        planId: asString(json['plan_id']) ?? 'free',
        desktop: asBool(json['desktop']) ?? false,
        entries: asList(
          json['entries'],
        ).map(LightEntry.fromJson).whereType<LightEntry>().toList(),
      );

  final String version;
  final String uiMode;
  final String planId;
  final bool desktop;
  final List<LightEntry> entries;

  bool get paidPlan => planId.isNotEmpty && planId != 'free';

  LightEntry? byId(String? id) {
    if (id == null) return null;
    for (final entry in entries) {
      if (entry.id == id) return entry;
    }
    return null;
  }
}

/// `POST /api/light/start`: a real conversation with the opening card already
/// in it; everything after goes through the ordinary `prompt_async`.
class LightStartResult {
  const LightStartResult({
    required this.sessionId,
    this.entry,
    this.openingCard = const LightOpeningCard(),
    this.steps = const [],
    this.videoModel,
    this.videoResolution,
  });

  factory LightStartResult.fromJson(Map<String, dynamic> json) =>
      LightStartResult(
        sessionId: asString(json['session_id']) ?? '',
        entry: LightEntry.fromJson(json['entry']),
        openingCard: LightOpeningCard.fromJson(json['opening_card']),
        steps: asList(
          json['steps'],
        ).map(LightStep.fromJson).whereType<LightStep>().toList(),
        videoModel: asString(json['video_model']),
        videoResolution: asString(json['video_resolution']),
      );

  final String sessionId;
  final LightEntry? entry;
  final LightOpeningCard openingCard;
  final List<LightStep> steps;
  final String? videoModel;
  final String? videoResolution;
}

/// One image or video a conversation produced (`relation.role` final/result).
class LightRecentOutput {
  const LightRecentOutput({
    required this.assetId,
    required this.mime,
    this.kind = 'file',
    this.label = '',
    this.createdAt,
  });

  static LightRecentOutput? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final assetId = asString(json['asset_id']);
    if (assetId == null || assetId.isEmpty) return null;
    return LightRecentOutput(
      assetId: assetId,
      mime: asString(json['mime']) ?? '',
      kind: asString(json['kind']) ?? 'file',
      label: asString(json['label']) ?? '',
      createdAt: asDate(json['created_at']),
    );
  }

  final String assetId;
  final String mime;
  final String kind;
  final String label;
  final DateTime? createdAt;

  bool get isVideo => mime.startsWith('video/');
  bool get isImage => mime.startsWith('image/');
}

/// `GET /api/light/recent`: one group per conversation, newest first.
class LightRecentGroup {
  const LightRecentGroup({
    required this.sessionId,
    this.title = '',
    this.entryId,
    this.updatedAt,
    this.outputs = const [],
  });

  static LightRecentGroup? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final sessionId = asString(json['session_id']);
    if (sessionId == null || sessionId.isEmpty) return null;
    final entryId = asString(json['entry_id']);
    return LightRecentGroup(
      sessionId: sessionId,
      title: asString(json['title']) ?? '',
      entryId: entryId == null || entryId.isEmpty ? null : entryId,
      updatedAt: asDate(json['updated_at']),
      outputs: asList(json['outputs'])
          .map(LightRecentOutput.fromJson)
          .whereType<LightRecentOutput>()
          .toList(),
    );
  }

  final String sessionId;
  final String title;

  /// Set when the conversation was started from a Light entry; it then opens
  /// as a topic chat rather than a plain one.
  final String? entryId;
  final DateTime? updatedAt;
  final List<LightRecentOutput> outputs;
}

List<LightRecentGroup> lightRecentGroupsFromJson(Map<String, dynamic> json) =>
    asList(json['groups'])
        .map(LightRecentGroup.fromJson)
        .whereType<LightRecentGroup>()
        .toList();
