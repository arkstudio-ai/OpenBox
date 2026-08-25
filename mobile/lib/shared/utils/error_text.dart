import 'package:dio/dio.dart';

import '../api/api_error.dart';
import '../i18n/i18n.dart';

/// Maps any thrown error to a localized message via the `errors` namespace
/// (stable backend codes → `errors:<code>`, else `errors:fallback`),
/// mirroring frontend-v2's error mapping convention.
String errorText(I18nState i18n, Object error) {
  final apiError = error is DioException ? ApiError.fromDio(error) : error;
  if (apiError is ApiError) {
    if (apiError.code == 'NETWORK') return i18n.t('errors:network');
    final mapped = i18n.t('errors:${apiError.code}');
    if (mapped != 'errors:${apiError.code}') return mapped;
    if (apiError.message.isNotEmpty) return apiError.message;
  }
  return i18n.t('errors:fallback');
}
