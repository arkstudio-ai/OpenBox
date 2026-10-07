part of 'admin_api.dart';

/// Explicit reads and audited writes. A write's retry key belongs to its caller.
extension AdminBillingApi on AdminApi {
  Future<AdminPage> subscriptions(
    Map<String, dynamic> query,
    CancelToken cancel,
  ) => _page('/api/admin/billing/subscriptions', query, cancel);
  Future<AdminPage> orders(Map<String, dynamic> query, CancelToken cancel) =>
      _page('/api/admin/billing/orders', query, cancel);
  Future<AdminRecord> workspaceBilling(String id, CancelToken cancel) =>
      _record('/api/admin/billing/workspaces/${_id(id)}', cancel: cancel);
  Future<AdminRecord> paymentProviders(CancelToken cancel) =>
      _record('/api/billing/providers', cancel: cancel);

  Future<AdminRecord> manageBilling(
    String workspaceId,
    String kind,
    Map<String, dynamic> body,
    CancelToken cancel, {
    String? subscriptionId,
  }) {
    _reason(body['reason'] as String? ?? '');
    if (!{'credits', 'grant', 'change', 'cancel'}.contains(kind) ||
        (body['request_key'] as String? ?? '').isEmpty ||
        ({'change', 'cancel'}.contains(kind) &&
            (subscriptionId ?? '').isEmpty)) {
      throw ArgumentError('Invalid billing operation');
    }
    final base = '/api/admin/billing/workspaces/${_id(workspaceId)}';
    final path = switch (kind) {
      'credits' => '$base/credits',
      'grant' => '$base/subscriptions',
      'change' => '$base/subscriptions/${_id(subscriptionId!)}',
      _ => '$base/subscriptions/${_id(subscriptionId!)}/cancel',
    };
    return _record(
      path,
      method: kind == 'change' ? 'PATCH' : 'POST',
      data: body,
      cancel: cancel,
    );
  }
}
