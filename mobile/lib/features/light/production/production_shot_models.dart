import '../../../shared/models/json.dart';
import 'production_api.dart' show ProductionMedia;

class ShotFrame {
  ShotFrame.fromJson(Map<String, dynamic> json)
    : url = asString(json['url']),
      source = asString(json['source']);

  final String? url;
  final String? source;

  static ShotFrame? from(Object? json) =>
      json == null ? null : ShotFrame.fromJson(asMap(json));
}

class ShotVersion {
  ShotVersion.fromJson(Map<String, dynamic> json)
    : id = asString(json['id']) ?? '',
      number = asInt(json['number']) ?? 1,
      prompt = asString(json['prompt']) ?? '',
      createdAt = DateTime.tryParse(asString(json['created_at']) ?? ''),
      media = ProductionMedia.fromJson(asMap(json['media'])),
      first = ShotFrame.from(json['first_frame']),
      last = ShotFrame.from(json['last_frame']),
      framePrompts = {
        for (final entry in asMap(json['frame_prompts']).entries)
          entry.key: ?asString(entry.value),
      };

  final String id, prompt;
  final int number;
  final DateTime? createdAt;
  final ProductionMedia media;
  final ShotFrame? first, last;
  final Map<String, String> framePrompts;

  static ShotVersion? from(Object? json) =>
      json == null ? null : ShotVersion.fromJson(asMap(json));
}

String shotFrameRole(bool tail) => tail ? 'last_frame' : 'first_frame';

class ShotFrameDraft {
  ShotFrameDraft.fromJson(Map<String, dynamic> json)
    : prompt = asString(json['prompt']) ?? '',
      status = asString(json['status']) ?? '',
      useCurrent = json['use_current'] != false,
      media = json['media'] != null
          ? ProductionMedia.fromJson(asMap(json['media']))
          : null;

  final String prompt, status;
  final bool useCurrent;
  final ProductionMedia? media;
}

class ProductionShot {
  ProductionShot.fromJson(Map<String, dynamic> json)
    : index = asInt(json['index']) ?? 0,
      title = asString(json['title']) ?? '',
      status = asString(json['status']) ?? 'waiting',
      prompt = asString(json['prompt']) ?? '',
      editable = json['editable'] == true,
      frameEditable = json['frame_editable'] == true,
      pending = json['pending'] == true,
      titleHidden = json['title_hidden'] as bool?,
      titlePending = json['title_pending'] == true,
      frameDrafts = {
        for (final entry in asMap(json['frame_drafts']).entries)
          entry.key: ShotFrameDraft.fromJson(asMap(entry.value)),
      },
      version = ShotVersion.from(json['version']),
      versions = asList(
        json['versions'],
      ).map((value) => ShotVersion.fromJson(asMap(value))).toList(),
      first = ShotFrame.from(json['first_frame']),
      last = ShotFrame.from(json['last_frame']);

  final int index;
  final String title, status, prompt;
  final bool editable, frameEditable, pending;
  final bool? titleHidden;
  final bool titlePending;
  final Map<String, ShotFrameDraft> frameDrafts;
  final ShotVersion? version;
  final List<ShotVersion> versions;
  final ShotFrame? first, last;
  ShotFrameDraft? draft(bool tail) => frameDrafts[shotFrameRole(tail)];
}

class FilmVersion {
  FilmVersion.fromJson(Map<String, dynamic> json)
    : number = asInt(json['number']) ?? 1,
      current = json['current'] == true,
      media = ProductionMedia.fromJson(asMap(json['media'])),
      createdAt = DateTime.tryParse(asString(json['created_at']) ?? ''),
      duration = asDouble(json['duration_sec']) ?? 0,
      segments = {
        for (final segment in asList(json['segments']).map(asMap))
          ?asInt(segment['index']): ?asString(segment['version_id']),
      };

  final int number;
  final bool current;
  final ProductionMedia media;
  final DateTime? createdAt;
  final double duration;
  final Map<int, String> segments;
}

class ProductionShotWorkspace {
  ProductionShotWorkspace.fromJson(Map<String, dynamic> json)
    : title = asString(json['title']) ?? '',
      shots = asList(
        json['shots'],
      ).map((value) => ProductionShot.fromJson(asMap(value))).toList(),
      films = asList(
        json['films'],
      ).map((value) => FilmVersion.fromJson(asMap(value))).toList(),
      active = json['active'] == true,
      canCompose = json['can_compose'] == true,
      composeState = asString(json['compose_state']);

  final String title;
  final List<ProductionShot> shots;
  final List<FilmVersion> films;
  final bool active, canCompose;
  final String? composeState;
  Iterable<ProductionShot> get pending => shots.where((shot) => shot.pending);
  Iterable<ProductionShot> get titleEdits =>
      shots.where((shot) => shot.titlePending);
  FilmVersion? get current => films.where((film) => film.current).firstOrNull;
  bool get composing => const {
    'dispatching',
    'submitting',
    'in_progress',
    'queued',
  }.contains(composeState);
}
