import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/models/first_video.dart';
import '../api/first_video_api.dart';
import '../api/photo_picker.dart';
import 'first_video_store.dart';

/// What the six screens collect. Every field is optional: the flow must
/// reach a video without a single character typed.
class FirstVideoDraft {
  const FirstVideoDraft({
    this.industry,
    this.shopName = '',
    this.headline = '',
    this.template = 'signature',
    this.photo,
    this.assetId,
  });

  final String? industry;
  final String shopName;
  final String headline;
  final String template;

  /// Picked but not yet uploaded.
  final PickedPhoto? photo;

  /// Uploaded reference image, once [FirstVideoFlow.submit] has sent it.
  final String? assetId;

  FirstVideoDraft copyWith({
    String? industry,
    String? shopName,
    String? headline,
    String? template,
    PickedPhoto? photo,
    bool clearPhoto = false,
    String? assetId,
  }) => FirstVideoDraft(
    industry: industry ?? this.industry,
    shopName: shopName ?? this.shopName,
    headline: headline ?? this.headline,
    template: template ?? this.template,
    photo: clearPhoto ? null : (photo ?? this.photo),
    assetId: clearPhoto ? null : (assetId ?? this.assetId),
  );
}

class FirstVideoDraftController extends Notifier<FirstVideoDraft> {
  @override
  FirstVideoDraft build() => const FirstVideoDraft();

  void setIndustry(String? industry) =>
      state = state.copyWith(industry: industry);
  void setShopName(String value) => state = state.copyWith(shopName: value);
  void setHeadline(String value) => state = state.copyWith(headline: value);
  void setTemplate(String template) =>
      state = state.copyWith(template: template);
  void setPhoto(PickedPhoto? photo) => state = photo == null
      ? state.copyWith(clearPhoto: true)
      : state.copyWith(photo: photo);
  void setAssetId(String assetId) => state = state.copyWith(assetId: assetId);
  void reset() => state = const FirstVideoDraft();
}

final firstVideoDraftProvider =
    NotifierProvider<FirstVideoDraftController, FirstVideoDraft>(
      FirstVideoDraftController.new,
    );

/// The job as last seen by any screen of the flow (submit or a poll), so
/// the making page can paint its first frame before its first poll answers.
final firstVideoJobProvider = StateProvider<FirstVideoJob?>((_) => null);

/// Upload the reference photo (if any) and submit the job. One call does
/// both so the photo screen has a single busy state to show.
class FirstVideoFlow {
  const FirstVideoFlow(this._ref);

  final Ref _ref;

  Future<FirstVideoJob> submit({
    bool withPhoto = true,
    void Function(double fraction)? onUploadProgress,
  }) async {
    final api = _ref.read(firstVideoApiProvider);
    var draft = _ref.read(firstVideoDraftProvider);
    var assetId = withPhoto ? draft.assetId : null;
    final photo = draft.photo;
    if (withPhoto && assetId == null && photo != null) {
      assetId = await api.uploadPhoto(
        name: photo.name,
        mime: photo.mime,
        bytes: photo.bytes,
        onProgress: onUploadProgress,
      );
      _ref.read(firstVideoDraftProvider.notifier).setAssetId(assetId);
      draft = _ref.read(firstVideoDraftProvider);
    }
    final job = await api.submit(
      template: draft.template,
      industry: draft.industry ?? 'other',
      shopName: draft.shopName,
      headline: draft.headline,
      assetId: assetId,
    );
    _ref.read(firstVideoJobProvider.notifier).state = job;
    _ref
        .read(firstVideoProvider.notifier)
        .noteJob(
          job,
          template: draft.template,
          shopName: draft.shopName.trim().isEmpty ? null : draft.shopName,
          headline: draft.headline.trim().isEmpty ? null : draft.headline,
        );
    return job;
  }

  /// One poll of the job; also remembered for the next screen.
  Future<FirstVideoJob> refresh() async {
    final job = await _ref.read(firstVideoApiProvider).status();
    _ref.read(firstVideoJobProvider.notifier).state = job;
    _ref.read(firstVideoProvider.notifier).noteJob(job);
    return job;
  }
}

final firstVideoFlowProvider = Provider<FirstVideoFlow>(FirstVideoFlow.new);
