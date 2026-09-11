part of 'admin_api.dart';

/// 消息通知: first-party announcements and topic pages
/// (`/api/admin/messages`, docs/MESSAGE_CENTER.md §超管接口).
extension AdminMessagesApi on AdminApi {
  Future<AdminPage> announcements(CancelToken cancel) async {
    final record = await _record(
      '/api/admin/messages/announcements',
      query: {'limit': 200},
      cancel: cancel,
    );
    return AdminPage(
      items: record.records('items'),
      total: record.records('items').length,
      offset: 0,
      limit: 200,
    );
  }

  Future<AdminRecord> announcement(String id, [CancelToken? cancel]) =>
      _record('/api/admin/messages/announcements/${_id(id)}', cancel: cancel);

  Future<AdminRecord> createAnnouncement(Map<String, dynamic> body) =>
      _record('/api/admin/messages/announcements', method: 'POST', data: body);

  Future<AdminRecord> updateAnnouncement(
    String id,
    Map<String, dynamic> body,
  ) => _record(
    '/api/admin/messages/announcements/${_id(id)}',
    method: 'PUT',
    data: body,
  );

  Future<AdminRecord> publishAnnouncement(String id, CancelToken cancel) =>
      _record(
        '/api/admin/messages/announcements/${_id(id)}/publish',
        method: 'POST',
        cancel: cancel,
      );

  Future<AdminRecord> revokeAnnouncement(String id, CancelToken cancel) =>
      _record(
        '/api/admin/messages/announcements/${_id(id)}/revoke',
        method: 'POST',
        cancel: cancel,
      );

  Future<AdminRecord> previewAnnouncement(String id) => _record(
    '/api/admin/messages/announcements/${_id(id)}/preview',
    method: 'POST',
  );

  Future<AdminPage> topics(CancelToken cancel) async {
    final record = await _record(
      '/api/admin/messages/topics',
      query: {'limit': 500},
      cancel: cancel,
    );
    return AdminPage(
      items: record.records('items'),
      total: record.records('items').length,
      offset: 0,
      limit: 500,
    );
  }

  Future<AdminRecord> setTopicPublished(
    String id,
    bool published,
    CancelToken cancel,
  ) => _record(
    '/api/admin/messages/topics/${_id(id)}/${published ? 'publish' : 'unpublish'}',
    method: 'POST',
    cancel: cancel,
  );
}
