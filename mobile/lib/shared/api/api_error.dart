import 'package:dio/dio.dart';

/// Normalized API error, mirroring frontend-v2 `shared/api/http.ts` ApiError:
/// backend returns FastAPI `{detail}` (+ optional `code`); we map to
/// `{status, code, message, meta}`.
class ApiError implements Exception {
  ApiError({
    required this.status,
    required this.code,
    required this.message,
    this.meta = const {},
  });

  factory ApiError.fromDio(DioException e) {
    final status = e.response?.statusCode ?? 0;
    String code = 'HTTP_$status';
    String message = e.response?.statusMessage ?? e.message ?? 'Network error';
    var meta = const <String, dynamic>{};
    final body = e.response?.data;
    if (body is Map<String, dynamic>) {
      final rawCode = body['code'];
      if (rawCode is String) code = rawCode;
      final detail = body['detail'];
      if (detail is String) {
        message = detail;
      } else if (detail is Map<String, dynamic>) {
        // FastAPI puts a structured refusal under `detail`. Quota replies use
        // it to carry a code and the two numbers; reading only the top level
        // left every quota looking like a bare HTTP_429.
        final innerCode = detail['code'];
        if (innerCode is String) code = innerCode;
        final innerMessage = detail['message'];
        message = innerMessage is String ? innerMessage : detail.toString();
        meta = detail;
      } else if (detail != null) {
        message = detail.toString();
      }
    }
    if (status == 0) code = 'NETWORK';
    return ApiError(status: status, code: code, message: message, meta: meta);
  }

  final int status;
  final String code;
  final String message;

  /// Numbers a quota refusal carries, so the copy can say how far over it is.
  final Map<String, dynamic> meta;

  @override
  String toString() => 'ApiError($status $code): $message';
}
