part of 'admin_api.dart';

/// Intentionally GET-only, with no timers or focus-triggered audit writes.
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
}
