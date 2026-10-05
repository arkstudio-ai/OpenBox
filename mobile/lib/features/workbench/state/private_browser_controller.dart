import 'dart:async';
import 'dart:math';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/private_browser_api.dart';

String _identity() =>
    'mobile-${List.generate(24, (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0')).join()}';

/// One mounted Session/actor/workspace. Tokens and frames exist only here.
/// A canceled HTTP wait does not prove remote cancellation. Unknown input is
/// never submitted again; only status can be read until an explicit handover.
class PrivateBrowserController extends ChangeNotifier {
  PrivateBrowserController(this.api, {DateTime Function()? now})
    : _now = now ?? DateTime.now;
  final PrivateBrowserApi api;
  final DateTime Function() _now;
  BrowserSnapshot? _resource;
  BrowserGrant? _grant;
  BrowserFrame? _frame;
  BrowserControlCommand? _pending;
  String? _bindingId;
  bool _loaded = false, _error = false, _unconfirmed = false;
  bool _visible = true, _disposed = false, _refreshing = false;
  bool _controlling = false, _renewing = false;
  int _version = 0, _readVersion = 0, _actionVersion = 0, _grantVersion = 0;
  int _busy = 0;
  CancelToken _cancel = CancelToken();
  Timer? _poll, _heartbeat, _expiry;
  ({int resumed, int changed})? _givenBack;
  String? _unconfirmedOperationId;

  BrowserSnapshot? get resource => _resource;
  BrowserFrame? get frame => controlled ? _frame : null;
  BrowserControlCommand? get pending => _pending;
  bool get loaded => _loaded;
  bool get error => _error;
  bool get busy => _busy > 0;
  bool get controlling => _controlling;
  bool get unconfirmed => _unconfirmed;
  String? get unconfirmedOperationId => _unconfirmedOperationId;
  ({int resumed, int changed})? get givenBack => _givenBack;
  bool get canPrepare => _loaded && !_error && _bindingId == null;
  bool get _current => !_disposed && _visible && api.isCurrent;
  bool _currentVersion(int version) => _current && version == _version;
  bool _currentAction(int version, int action) =>
      _currentVersion(version) && action == _actionVersion;
  bool get controlled =>
      _current &&
      _grant != null &&
      _resource != null &&
      _active(_resource!, _grant!);
  bool get canInput => controlled && !busy && !_unconfirmed && _frame != null;

  bool _active(BrowserSnapshot row, BrowserGrant grant) =>
      row.remoteAvailable &&
      row.status == 'active' &&
      row.admission == 'open' &&
      row.fence == grant.fence &&
      grant.expiresAt.isAfter(_now());

  void _emit() {
    if (!_disposed) notifyListeners();
  }

  void _clearGrant() {
    _grantVersion++;
    _grant = null;
    _frame = null;
    _heartbeat?.cancel();
    _expiry?.cancel();
    _heartbeat = _expiry = null;
  }

  Future<void> start() async {
    if (!_current) return;
    _poll ??= Timer.periodic(const Duration(seconds: 5), (_) {
      if (!_current) {
        _clearGrant();
        _emit();
      } else if (!busy) {
        unawaited(refresh());
      }
    });
    await refresh();
  }

  /// No implicit giveback on backgrounding, expiry, route close or logout.
  /// On return we read status without reconstructing a lost human token.
  void setVisible(bool visible, {bool notify = true}) {
    if (_disposed || _visible == visible) return;
    _visible = visible;
    _version++;
    _cancel.cancel();
    _cancel = CancelToken();
    _clearGrant();
    _poll?.cancel();
    _poll = null;
    _busy = 0;
    _controlling = _renewing = _refreshing = false;
    if (notify) _emit();
    if (visible) unawaited(start());
  }

  void _snapshot(BrowserSnapshot? next) {
    if (_bindingId != null && next != null && next.resourceId != _bindingId) {
      throw StateError('Original browser binding changed');
    }
    if (next != null) _bindingId ??= next.resourceId;
    final remotePending = next?.pending;
    if (remotePending != null) {
      if (_pending != null && !_pending!.sameRequest(remotePending)) {
        throw StateError('A different browser handover is pending');
      }
      _pending = remotePending;
    }
    _resource = next;
    _loaded = true;
    if (_grant != null && (next == null || !_active(next, _grant!))) {
      _clearGrant();
    }
  }

  Future<void> refresh({bool force = false}) async {
    if (!_current || (_refreshing && !force)) return;
    final version = _version, read = ++_readVersion, action = _actionVersion;
    _refreshing = true;
    try {
      final next = await api.current(_cancel);
      if (!_currentVersion(version) ||
          read != _readVersion ||
          action != _actionVersion) {
        return;
      }
      _snapshot(next);
      _error = false;
    } catch (_) {
      if (_currentVersion(version) &&
          read == _readVersion &&
          action == _actionVersion) {
        _resource = null;
        _loaded = true;
        _clearGrant();
        _error = true;
      }
    } finally {
      if (version == _version && read == _readVersion) _refreshing = false;
      _emit();
    }
  }

  Future<void> _run(
    Future<void> Function(int version, int action) perform, {
    bool control = false,
  }) async {
    if (!_current || (control ? _controlling : busy)) return;
    final version = _version, action = ++_actionVersion;
    _readVersion++;
    _refreshing = false;
    _busy++;
    if (control) _controlling = true;
    _error = false;
    _emit();
    try {
      await perform(version, action);
    } catch (_) {
      if (_currentAction(version, action)) {
        _clearGrant();
        _error = true;
      }
    } finally {
      if (version == _version) {
        _busy--;
        if (control) _controlling = false;
      }
      _emit();
    }
  }

  Future<void> ensure() async {
    if (!canPrepare) return;
    await _run((version, action) async {
      final next = await api.ensure(_cancel);
      if (_currentAction(version, action)) _snapshot(next);
    });
  }

  Future<void> control(String action, {bool continueOriginal = false}) async {
    if (!{'takeover', 'giveback', 'close'}.contains(action)) return;
    final row = _resource;
    final original = continueOriginal ? _pending : null;
    if (continueOriginal && (original == null || original.action != action)) {
      return;
    }
    if (!continueOriginal &&
        (_pending != null ||
            row == null ||
            (action == 'takeover' && !row.canTakeover) ||
            (action == 'giveback' && !row.canGiveback))) {
      return;
    }
    final command =
        original ??
        BrowserControlCommand(
          resourceId: row!.resourceId,
          action: action,
          expectedEpoch: row.fence.epoch,
          idempotencyKey: _identity(),
        );
    await _run((version, serial) async {
      _pending = command;
      _clearGrant();
      _givenBack = null;
      _emit();
      // The only replay is an explicit user request for this same command.
      // The backend may resume that accepted command; this is not a GET.
      final receipt = await api.control(command, _cancel);
      if (!_currentAction(version, serial)) return;
      final id = receipt['command_id'];
      if (id is! String ||
          id.isEmpty ||
          (command.commandId != null && command.commandId != id)) {
        throw const FormatException('Unconfirmed original handover');
      }
      _pending = BrowserControlCommand(
        resourceId: command.resourceId,
        action: command.action,
        expectedEpoch: command.expectedEpoch,
        idempotencyKey: command.idempotencyKey,
        commandId: id,
      );
      if (receipt['state'] == 'draining') {
        await refresh(force: true);
        return;
      }
      if (receipt['state'] != 'applied') {
        throw const FormatException('Unconfirmed handover');
      }
      final fence = BrowserFence.fromJson(browserMap(receipt['fence']));
      final expectedEpoch = command.expectedEpoch + (action == 'close' ? 0 : 1);
      if (fence.resourceId != command.resourceId ||
          fence.epoch != expectedEpoch ||
          (action == 'takeover' &&
              (fence.ownerKind != 'human' ||
                  fence.ownerId != api.scope.userId)) ||
          (action == 'giveback' &&
              (fence.ownerKind != 'automation' ||
                  fence.ownerId != api.scope.workspaceId))) {
        throw const FormatException('Handover fence changed');
      }
      _pending = null;
      if (action == 'takeover' && receipt['human_grant_expired'] != true) {
        final token = receipt['human_token'], expiry = receipt['expires_at'];
        final expiresAt = expiry is String ? DateTime.tryParse(expiry) : null;
        if (token is! String ||
            token.isEmpty ||
            expiresAt == null ||
            !expiresAt.isAfter(_now())) {
          throw const FormatException('Unavailable human grant');
        }
        _grant = BrowserGrant(fence, token, expiresAt);
        _grantVersion++;
        _scheduleGrant();
        await refresh(force: true);
        if (_currentAction(version, serial) && controlled) {
          await _capture(version, serial);
        }
      } else {
        if (action == 'giveback') {
          _unconfirmed = false;
          _unconfirmedOperationId = null;
          _givenBack = (
            resumed:
                (receipt['resume_requested_task_ids'] as List?)?.length ?? 0,
            changed:
                (receipt['task_control_changed_ids'] as List?)?.length ?? 0,
          );
        }
        await refresh(force: true);
      }
    }, control: true);
  }

  void _scheduleGrant() {
    _expiry?.cancel();
    final grant = _grant;
    if (grant == null) return;
    final remaining = grant.expiresAt.difference(_now());
    _expiry = Timer(remaining > Duration.zero ? remaining : Duration.zero, () {
      _clearGrant();
      _emit();
    });
    _heartbeat ??= Timer.periodic(const Duration(seconds: 30), (_) {
      unawaited(_renew());
    });
  }

  Future<void> _renew() async {
    if (!controlled || _renewing || _controlling || _unconfirmed) return;
    final version = _version, generation = _grantVersion, saved = _grant!;
    _renewing = true;
    try {
      final receipt = await api.heartbeat(saved, _identity(), _cancel);
      if (!_currentVersion(version) || generation != _grantVersion) return;
      final expiry = receipt['expires_at'];
      final expiresAt = expiry is String ? DateTime.tryParse(expiry) : null;
      if (BrowserFence.fromJson(browserMap(receipt['fence'])) != saved.fence ||
          expiresAt == null ||
          !expiresAt.isAfter(_now()) ||
          !controlled) {
        throw const FormatException('Expired browser control');
      }
      _grant = BrowserGrant(saved.fence, saved.token, expiresAt);
      _scheduleGrant();
    } catch (_) {
      if (_currentVersion(version) && generation == _grantVersion) {
        _clearGrant();
        _error = true;
      }
    } finally {
      if (version == _version) _renewing = false;
      _emit();
    }
  }

  Future<Map<String, dynamic>?> _operation(
    String kind,
    Map<String, dynamic> args,
    int version,
    int serial,
  ) async {
    if (!controlled) return null;
    final saved = _grant!, generation = _grantVersion, id = _identity();
    try {
      final receipt = await api.operation(saved, id, kind, args, _cancel);
      if (!_currentAction(version, serial) ||
          generation != _grantVersion ||
          !controlled) {
        return null;
      }
      if (receipt['operation_id'] != id ||
          receipt['state'] != 'completed' ||
          BrowserFence.fromJson(browserMap(receipt['fence'])) != saved.fence) {
        throw const FormatException('Unconfirmed browser operation');
      }
      final result = browserMap(receipt['result']);
      if (result['navigation_error'] != null) {
        throw const FormatException('Unconfirmed navigation');
      }
      return result;
    } catch (_) {
      if (_currentAction(version, serial) && generation == _grantVersion) {
        _unconfirmed = true;
        _unconfirmedOperationId = id;
        _clearGrant();
      }
      rethrow;
    }
  }

  Future<void> _capture(int version, int serial) async {
    _frame = null;
    final result = await _operation('capture', {}, version, serial);
    if (result == null || !controlled || !_currentAction(version, serial)) {
      return;
    }
    _frame = BrowserFrame.fromReceipt(result, _grant!.fence);
  }

  void rejectFrame(BrowserFrame frame) {
    if (!identical(_frame, frame)) return;
    _clearGrant();
    _error = true;
    _emit();
  }

  Future<void> operate(
    String kind, [
    Map<String, dynamic> args = const {},
  ]) async {
    if (!privateBrowserOperations.contains(kind) ||
        !controlled ||
        _unconfirmed ||
        (kind != 'capture' && !canInput)) {
      return;
    }
    await _run((version, serial) async {
      if (kind == 'capture') {
        await _capture(version, serial);
        return;
      }
      _frame = null;
      _emit();
      final result = await _operation(kind, args, version, serial);
      if (result != null && _currentAction(version, serial) && controlled) {
        await _capture(version, serial);
      }
    });
  }

  @override
  void dispose() {
    _disposed = true;
    _version++;
    _cancel.cancel();
    _poll?.cancel();
    _clearGrant();
    super.dispose();
  }
}

final privateBrowserControllerProvider = ChangeNotifierProvider.autoDispose
    .family<PrivateBrowserController, PrivateBrowserScope>((ref, scope) {
      final controller = PrivateBrowserController(
        ref.watch(privateBrowserApiProvider(scope)),
      );
      Future.microtask(controller.start);
      return controller;
    });
