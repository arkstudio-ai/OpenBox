part of 'admin_api.dart';

extension AdminFleetApi on AdminApi {
  Future<FleetData> fleet(
    CancelToken cancel, {
    Map<String, dynamic> desktopQuery = const {'offset': 0, 'limit': 20},
    int alertOffset = 0,
  }) async {
    final results = await Future.wait([
      _record('/api/admin/fleet/pool', cancel: cancel),
      _record('/api/admin/fleet/desktops', query: desktopQuery, cancel: cancel),
      _record(
        '/api/admin/fleet/alerts',
        query: {'state': 'open', 'offset': alertOffset, 'limit': 50},
        cancel: cancel,
      ),
      _record('/api/admin/fleet/snapshots/latest', cancel: cancel),
    ]);
    return FleetData(
      results[0],
      AdminPage.fromJson(results[1].data),
      AdminPage.fromJson(results[2].data),
      results[3],
    );
  }

  Future<AdminRecord> ensurePool(bool dryRun, CancelToken cancel) => _record(
    '/api/admin/fleet/pool/ensure',
    method: 'POST',
    query: {'dry_run': dryRun},
    cancel: cancel,
  );

  Future<void> desktopAction(
    String id,
    String action,
    CancelToken cancel,
  ) async {
    if (!{'release', 'recycle', 'retire'}.contains(action)) {
      throw ArgumentError('Unknown fleet action');
    }
    await _request(
      '/api/admin/fleet/desktops/${_id(id)}/$action',
      method: 'POST',
      data: action == 'recycle' ? {'approve': true} : null,
      cancel: cancel,
    );
  }

  Future<void> adoptDesktop(
    String id, {
    required String poolState,
    required bool rebuild,
    required bool gatewayReleaseVerified,
    required CancelToken cancel,
  }) async {
    if (id.trim().isEmpty || !{'reserve', 'prewarm'}.contains(poolState)) {
      throw ArgumentError('Invalid adoption');
    }
    await _request(
      '/api/admin/fleet/desktops/${_id(id.trim())}/adopt',
      method: 'POST',
      data: {
        'pool_state': poolState,
        'rebuild': rebuild,
        'approve': rebuild,
        'gateway_release_verified': gatewayReleaseVerified,
      },
      cancel: cancel,
    );
  }

  Future<void> acknowledgeAlert(String id, CancelToken cancel) async =>
      _request(
        '/api/admin/fleet/alerts/${_id(id)}/ack',
        method: 'POST',
        cancel: cancel,
      );
  Future<void> muteAlert(String id, DateTime until, CancelToken cancel) async =>
      _request(
        '/api/admin/fleet/alerts/${_id(id)}/mute',
        method: 'POST',
        data: {'until': until.toUtc().toIso8601String()},
        cancel: cancel,
      );

  Future<AdminPage> desktopEvents(String id, CancelToken cancel) => _page(
    '/api/admin/fleet/desktops/${_id(id)}/events',
    {'limit': 200},
    cancel,
  );
  Future<AdminPage> desktopDiagnostics(String id, CancelToken cancel) => _page(
    '/api/admin/fleet/diag/recent',
    {'desktop_id': id, 'limit': 20},
    cancel,
  );
  Future<AdminRecord> diagnostic(String id, CancelToken cancel) =>
      _record('/api/admin/fleet/diag/${_id(id)}', cancel: cancel);
  Future<AdminRecord> collectDiagnostic(String id, CancelToken cancel) =>
      _record(
        '/api/admin/fleet/desktops/${_id(id)}/diag',
        method: 'POST',
        data: {'via': 'auto', 'lines': 60},
        cancel: cancel,
      );
}
