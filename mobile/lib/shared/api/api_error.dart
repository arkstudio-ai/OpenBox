import 'package:dio/dio.dart';

/// Normalized API error, mirroring frontend-v2 `shared/api/http.ts` ApiError:
/// backend returns FastAPI `{detail}` (+ optional `code`); we map to
/// `{status, code, message}`.
class ApiError implements Exception {
  ApiError({required this.status, required this.code, required this.message});

  factory ApiError.fromDio(DioException e) {
    final status = e.response?.statusCode ?? 0;
    String code = 'HTTP_$status';
    String message = e.response?.statusMessage ?? e.message ?? 'Network error';
    final body = e.response?.data;
    if (body is Map<String, dynamic>) {
      final rawCode = body['code'];
      if (rawCode is String) code = rawCode;
      final detail = body['detail'];
      if (detail is String) {
        message = detail;
      } else if (detail != null) {
        message = detail.toString();
      }
    }
    if (status == 0) code = 'NETWORK';
    return ApiError(status: status, code: code, message: message);
  }

  final int status;
  final String code;
  final String message;

  @override
  String toString() => 'ApiError($status $code): $message';
}
