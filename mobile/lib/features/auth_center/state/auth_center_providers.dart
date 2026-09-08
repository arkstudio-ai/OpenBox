import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/platform_accounts_api.dart';
import '../../../shared/models/platform_account.dart';
import '../../../shared/models/resource.dart';

CancelToken _cancelOnDispose(Ref ref) {
  final cancel = CancelToken();
  ref.onDispose(cancel.cancel);
  return cancel;
}

final platformsProvider = FutureProvider.autoDispose
    .family<List<PlatformInfo>, PlatformScope>(
      (ref, scope) => ref
          .watch(platformAccountsApiProvider)
          .platforms(scope, cancel: _cancelOnDispose(ref)),
    );
final platformAccountsProvider = FutureProvider.autoDispose
    .family<List<PlatformAccount>, PlatformScope>(
      (ref, scope) => ref
          .watch(platformAccountsApiProvider)
          .accounts(scope, cancel: _cancelOnDispose(ref)),
    );
final publishJobsProvider = FutureProvider.autoDispose
    .family<List<PublishJob>, PlatformScope>(
      (ref, scope) => ref
          .watch(platformAccountsApiProvider)
          .jobs(scope, cancel: _cancelOnDispose(ref)),
    );
final publishVideosProvider = FutureProvider.autoDispose
    .family<ResourcePage, PlatformScope>(
      (ref, scope) => ref
          .watch(platformAccountsApiProvider)
          .videos(scope, cancel: _cancelOnDispose(ref)),
    );

bool isPublishableVideo(Resource video) =>
    video.status == 'ready' &&
    video.size > 0 &&
    video.size <= 128 * 1024 * 1024 &&
    const {
      'video/mp4',
      'video/quicktime',
      'video/3gpp',
    }.contains(video.mime.split(';').first.trim().toLowerCase());

List<String> parsePublishHashtags(String raw) => raw
    .split(RegExp(r'[\s,，#]+'))
    .map((v) => v.trim())
    .where((v) => v.isNotEmpty)
    .toSet()
    .take(10)
    .toList();
