import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/providers.dart';
import '../i18n/i18n.dart';
import '../models/json.dart';
import '../utils/error_text.dart';
import '../widgets/toast.dart';
import 'native_download.dart';

/// `ref.read` or `container.read`: whichever the caller has.
typedef ProviderReader = T Function<T>(ProviderListenable<T> provider);

/// `/api/assets/<id>/download?token=…` or `/api/assets/<id>/url`, with or
/// without the API host. Replies used to paste the tool's tokenised download
/// link; the token died within a day but the asset did not, so such a link is
/// honoured by asset id through the authenticated download path.
final _assetLink = RegExp(
  r'^(?:https?://[^/]+)?/api/assets/(asset_[A-Za-z0-9]+)/(?:download|url)(?:[?#].*)?$',
);

String? assetIdFromLink(String url) =>
    _assetLink.firstMatch(url.trim())?.group(1);

enum AssetSaveTarget { files, album }

enum AssetSaveOutcome { saved, cancelled, failed }

/// One path for every "get this file onto my phone" entry point: the viewer
/// buttons, file chips and asset links the model pasted into Markdown.
///
/// Trades the owned asset id for a fresh signed URL (the stored one expires),
/// downloads the bytes, hands them to the album or the system picker, and
/// always says where they went. The old flow was silent on success, so people
/// pressed download again and again and reported that it "did nothing".
Future<AssetSaveOutcome> saveAssetToDevice(
  ProviderReader read, {
  required String assetId,
  String name = '',
  String? mimeType,
  AssetSaveTarget target = AssetSaveTarget.files,
  void Function(double fraction)? onProgress,
}) async {
  final i18n = read(i18nProvider);
  final toast = read(toastProvider.notifier);
  try {
    final resp = await read(apiDioProvider).get<Map<String, dynamic>>(
      '/api/assets/$assetId/url',
      queryParameters: {'download': true},
    );
    final url = asString(resp.data?['url']);
    if (url == null) throw StateError('The server returned no download URL');
    final mime =
        mimeType ?? asString(resp.data?['mime']) ?? 'application/octet-stream';
    final fileName = name.isNotEmpty
        ? name
        : (asString(resp.data?['name']) ?? 'download');
    final native = read(nativeDownloadProvider);

    if (target == AssetSaveTarget.album) {
      try {
        await native.saveUrlToAlbum(
          url: url,
          suggestedName: fileName,
          mimeType: mime,
          onProgress: onProgress,
        );
        toast.success(i18n.t('chat:download.savedToAlbum'));
        return AssetSaveOutcome.saved;
      } on AlbumAccessDenied {
        // No photo permission: fall back to the system picker so the person
        // still gets the file, and say why the album was skipped.
        toast.info(i18n.t('chat:download.albumDenied'));
      }
    }

    final saved = await native.saveUrl(
      url: url,
      suggestedName: fileName,
      mimeType: mime,
      onProgress: onProgress,
    );
    if (!saved) return AssetSaveOutcome.cancelled;
    toast.success(i18n.t('chat:download.savedToFiles'));
    return AssetSaveOutcome.saved;
  } on StateError catch (error) {
    if (error.message.contains('already in progress')) {
      toast.info(i18n.t('chat:download.busy'));
    } else {
      toast.error(errorText(i18n, error));
    }
    return AssetSaveOutcome.failed;
  } catch (error) {
    toast.error(errorText(i18n, error));
    return AssetSaveOutcome.failed;
  }
}
