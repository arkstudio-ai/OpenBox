import 'package:dio/dio.dart';

import '../api/api_error.dart';
import '../i18n/i18n.dart';

/// Maps any thrown error to a localized message via the `errors` namespace
/// (stable backend codes → `errors:<code>`, else `errors:fallback`),
/// mirroring frontend-v2 `shared/hooks/useApiErrorMessage.ts`.
String errorText(I18nState i18n, Object error) {
  final apiError = error is DioException ? ApiError.fromDio(error) : error;
  if (apiError is ApiError) {
    if (apiError.code == 'NETWORK') return i18n.t('errors:network');
    // A quota refusal carries its two numbers; interpolate them so the copy
    // says how far over the line the person is rather than that a limit
    // exists somewhere.
    final mapped = i18n.t('errors:${apiError.code}', vars: {
      'used': apiError.meta['used'],
      'limit': apiError.meta['limit'],
    });
    if (mapped != 'errors:${apiError.code}') return mapped;
    final byStatus = i18n.t('errors:HTTP_${apiError.status}');
    if (byStatus != 'errors:HTTP_${apiError.status}') return byStatus;
    // Before giving up, use what the server said. Quota and validation
    // replies carry a specific reason ("Session quota exceeded: 200/200"),
    // and dropping it for a generic fallback hides the one detail that tells
    // someone what to do about it.
    final detail = apiError.message.trim();
    if (detail.isNotEmpty && detail != apiError.code) return detail;
  }
  return i18n.t('errors:fallback');
}

/// Copy for a run that ended in failure (web `useRunFailureMessage`).
/// Prefers a known code, falls back to whatever the server said, and only
/// then to the generic line — the upstream reason is the one thing that tells
/// someone what to fix.
String runFailureText(I18nState i18n, Map<String, dynamic>? error) {
  final code = error?['code'];
  if (code is String && code.isNotEmpty) {
    final byCode = i18n.t('errors:$code');
    if (byCode != 'errors:$code') return byCode;
  }
  final message = error?['message'];
  if (message is String && message.trim().isNotEmpty) return message.trim();
  return i18n.t('errors:runFailed');
}
