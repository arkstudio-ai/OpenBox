import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../api/admin_api.dart';

/// Each visible list owns its read, cancellation and generation. There is no
/// background fetch of billing, review archives or live desktop directories.
abstract class AdminLoadState<T, W extends ConsumerStatefulWidget>
    extends ConsumerState<W> {
  T? data;
  Object? loadError;
  bool loading = false;
  CancelToken? _request;
  int _generation = 0;
  bool get loadOnMount => true;
  AdminApi get api => ref.read(adminApiProvider);
  I18nState get i18n => ref.watch(i18nProvider);
  Future<T> fetch(CancelToken cancel);

  @override
  void initState() {
    super.initState();
    if (loadOnMount) scheduleMicrotask(reload);
  }

  Future<void> reload({bool clear = false}) async {
    if (!mounted) return;
    _request?.cancel('Superseded read');
    final token = CancelToken();
    final generation = ++_generation;
    _request = token;
    setState(() {
      loading = true;
      loadError = null;
      if (clear) data = null;
    });
    try {
      final result = await fetch(token);
      if (mounted && !token.isCancelled && generation == _generation) {
        setState(() => data = result);
      }
    } catch (error) {
      if (mounted && !token.isCancelled && generation == _generation) {
        setState(() => loadError = error);
      }
    } finally {
      if (mounted && generation == _generation) setState(() => loading = false);
    }
  }

  void clearRead() {
    _request?.cancel();
    _generation++;
    setState(() {
      data = null;
      loading = false;
      loadError = null;
    });
  }

  @override
  void dispose() {
    _request?.cancel('Admin page disposed');
    super.dispose();
  }

  Widget loadable(Widget Function(T) build) {
    final value = data;
    if (value == null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: loadError == null
              ? const CircularProgressIndicator()
              : SingleChildScrollView(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(
                        errorText(i18n, loadError!),
                        textAlign: TextAlign.center,
                        style: TextStyle(color: context.tokens.danger),
                      ),
                      TextButton.icon(
                        onPressed: reload,
                        icon: const Icon(Icons.refresh),
                        label: Text(i18n.t('common:action.retry')),
                      ),
                    ],
                  ),
                ),
        ),
      );
    }
    return Column(
      children: [
        if (loading) const LinearProgressIndicator(minHeight: 2),
        if (loadError != null)
          Padding(
            padding: const EdgeInsets.all(12),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxHeight: 96),
              child: SingleChildScrollView(
                child: Text(
                  errorText(i18n, loadError!),
                  style: TextStyle(color: context.tokens.danger),
                ),
              ),
            ),
          ),
        Expanded(child: build(value)),
      ],
    );
  }
}
