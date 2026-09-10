part of 'admin_api.dart';

extension AdminSkillsApi on AdminApi {
  String _storePath(String id) => '/api/admin/skills/store/${_id(id)}';
  String _reviewPath(String id) => '/api/admin/skills/review/${_id(id)}';

  Future<AdminPage> skillStore(
    Map<String, dynamic> query,
    CancelToken cancel,
  ) => _page('/api/admin/skills/store', query, cancel);
  Future<AdminPage> skillReviews(
    Map<String, dynamic> query,
    CancelToken cancel,
  ) => _page('/api/admin/skills/review', query, cancel);
  Future<AdminPage> skillInstalls(
    Map<String, dynamic> query,
    CancelToken cancel,
  ) => _page('/api/admin/skills/installs', query, cancel);
  Future<AdminRecord> skillDetail(String id, CancelToken cancel) =>
      _record(_storePath(id), cancel: cancel);
  Future<AdminRecord> reviewDetail(String id, CancelToken cancel) =>
      _record(_reviewPath(id), cancel: cancel);

  Future<void> setListing(
    String id,
    String listing,
    String note,
    CancelToken cancel,
  ) async {
    if (!{'listed', 'delisted'}.contains(listing)) {
      throw ArgumentError('Invalid listing');
    }
    if (listing == 'delisted') _reason(note);
    await _request(
      '${_storePath(id)}/listing',
      method: 'POST',
      data: {
        'listing': listing,
        if (note.trim().isNotEmpty) 'note': note.trim(),
      },
      cancel: cancel,
    );
  }

  Future<void> setFeatured(String id, bool value, CancelToken cancel) async =>
      _request(
        '${_storePath(id)}/featured',
        method: 'POST',
        data: {'featured': value},
        cancel: cancel,
      );
  Future<void> setOfficial(String id, bool value, CancelToken cancel) async {
    if (!id.startsWith('community:')) {
      throw ArgumentError('Only community entries support official status');
    }
    await _request(
      '${_storePath(id)}/official',
      method: 'POST',
      data: {'is_official': value},
      cancel: cancel,
    );
  }

  Future<void> reviewSkill(
    String id,
    bool approve,
    String note,
    CancelToken cancel,
  ) async {
    if (!id.startsWith('community:')) {
      throw ArgumentError('Only community submissions are reviewed');
    }
    if (!approve) _reason(note);
    final action = approve ? 'approve' : 'reject';
    await _request(
      '${_reviewPath(id)}/$action',
      method: 'POST',
      data: approve ? null : {'note': note.trim()},
      cancel: cancel,
    );
  }

  Future<void> saveSkill({
    String? id,
    required String name,
    required String kind,
    required int revision,
    required Map<String, dynamic> fields,
    required CancelToken cancel,
  }) async {
    await _request(
      id == null ? '/api/admin/skills/store' : _storePath(id),
      method: id == null ? 'POST' : 'PATCH',
      data: {
        ...fields,
        if (id == null) ...{
          'name': name,
          'kind': kind,
        } else
          'expected_revision': revision,
      },
      cancel: cancel,
    );
  }

  Future<AdminPage> deleteSkills(
    List<String> ids,
    String reason,
    CancelToken cancel,
  ) async {
    _reason(reason);
    if (ids.isEmpty) throw ArgumentError('No selected entries');
    final data = await _record(
      '/api/admin/skills/store/batch-delete',
      method: 'POST',
      data: {'catalog_ids': ids, 'reason': reason.trim()},
      cancel: cancel,
    );
    return AdminPage.fromJson(data.data);
  }

  Future<void> restoreSkill(String id, CancelToken cancel) async =>
      _request('${_storePath(id)}/restore', method: 'POST', cancel: cancel);

  Future<void> uploadSkill({
    required String filename,
    required int length,
    required Stream<List<int>> Function() openRead,
    required CancelToken cancel,
  }) async {
    final result = await _record(
      '/api/admin/skills/store/upload',
      method: 'POST',
      data: FormData.fromMap({
        'files': [
          MultipartFile.fromStream(openRead, length, filename: filename),
        ],
      }),
      cancel: cancel,
    );
    final items = result.records('items');
    if (items.length != 1 || !items.single.flag('ok')) {
      throw ApiError(
        status: 0,
        code: 'ARCHIVE_UPLOAD_FAILED',
        message: items.isEmpty
            ? 'Missing upload result'
            : items.first.string('error'),
      );
    }
  }

  Future<Stream<List<int>>> reviewArchive(String id, CancelToken cancel) async {
    final data = await _request(
      '${_reviewPath(id)}/archive',
      cancel: cancel,
      responseType: ResponseType.stream,
    );
    if (data is! ResponseBody) {
      throw const FormatException('Expected archive stream');
    }
    return _archiveStream(data, cancel);
  }

  Stream<List<int>> _archiveStream(
    ResponseBody body,
    CancelToken cancel,
  ) async* {
    _requests.update(cancel, (count) => count + 1, ifAbsent: () => 1);
    try {
      await for (final chunk in body.stream) {
        checkAccess();
        if (cancel.isCancelled) throw cancel.cancelError!;
        yield chunk;
      }
      checkAccess();
    } finally {
      final count = _requests[cancel] ?? 0;
      if (count <= 1) {
        _requests.remove(cancel);
      } else {
        _requests[cancel] = count - 1;
      }
    }
  }

  Future<AdminPage> installedDesktops(
    Map<String, dynamic> query,
    CancelToken cancel,
  ) => _page('/api/admin/skills/desktops', query, cancel);

  /// This endpoint scans a selected member directory. Never poll or prefetch.
  Future<AdminRecord> scanDesktop(
    String desktop,
    String user,
    CancelToken cancel,
  ) {
    if (user.isEmpty) throw ArgumentError('A workspace member is required');
    return _record(
      '/api/admin/skills/desktops/${_id(desktop)}/skills',
      query: {'user_id': user},
      cancel: cancel,
    );
  }

  Future<void> uninstallSkill(
    String desktop,
    String user,
    AdminRecord skill,
    String reason,
    CancelToken cancel,
  ) async {
    _reason(reason);
    if (user.isEmpty || !skill.flag('removable')) {
      throw ArgumentError('Protected installation');
    }
    await _request(
      '/api/admin/skills/desktops/${_id(desktop)}/uninstall',
      method: 'POST',
      data: {
        'user_id': user,
        'kind': skill.string('kind'),
        'install_dir': skill.string('install_dir'),
        'reason': reason.trim(),
      },
      cancel: cancel,
    );
  }
}
