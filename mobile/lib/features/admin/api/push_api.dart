part of 'admin_api.dart';

String newPushTestRequestId() {
  final random = Random.secure();
  final bytes = List.generate(16, (_) => random.nextInt(256));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}

extension AdminPushApi on AdminApi {
  Future<AdminRecord> pushOverview(String locale, CancelToken cancel) =>
      _record('/api/admin/push', query: {'locale': locale}, cancel: cancel);

  Future<AdminRecord> sendPushTest({
    required String template,
    required String bindingId,
    required String requestId,
  }) => _record(
    '/api/admin/push/test',
    method: 'POST',
    data: {
      'template': template,
      'bindingId': bindingId,
      'requestId': requestId,
    },
  );
}
