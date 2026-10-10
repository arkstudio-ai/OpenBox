import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gal/gal.dart';
import 'package:path_provider/path_provider.dart';

/// Downloads a file inside the app, then hands the completed temporary file to
/// Android's document creator or iOS' document exporter. Presigned object-store
/// URLs deliberately use a bare Dio client so API credentials never leave the
/// first-party origin.
class NativeDownloadService {
  NativeDownloadService({Dio? downloadClient})
    : _downloadClient =
          downloadClient ??
          Dio(
            BaseOptions(
              connectTimeout: const Duration(seconds: 30),
              receiveTimeout: const Duration(minutes: 2),
              followRedirects: true,
            ),
          );

  static const _channel = MethodChannel('com.bossip.bipmobile/download');

  final Dio _downloadClient;
  bool _saving = false;

  /// Export an authenticated stream without buffering an archive in memory.
  /// Re-check identity before each chunk and before opening the system picker.
  Future<bool> saveStream({
    required Stream<List<int>> stream,
    required String suggestedName,
    required void Function() checkAccess,
    String mimeType = 'application/zip',
  }) async {
    if (_saving) throw StateError('A native download is already in progress');
    _saving = true;
    Directory? staging;
    IOSink? sink;
    try {
      checkAccess();
      staging = await (await getTemporaryDirectory()).createTemp(
        'admin-archive-',
      );
      final name = nativeDownloadFileName(suggestedName);
      final file = File('${staging.path}/$name');
      sink = file.openWrite();
      await sink.addStream(
        stream.map((chunk) {
          checkAccess();
          return chunk;
        }),
      );
      await sink.close();
      sink = null;
      checkAccess();
      return await _channel.invokeMethod<bool>('saveFile', {
            'path': file.path,
            'name': name,
            'mimeType': mimeType,
          }) ??
          false;
    } finally {
      try {
        await sink?.close();
      } on FileSystemException {
        // addStream may already close the sink on a read failure. Preserve
        // the original stream/scope error while still cleaning our staging.
      } finally {
        _saving = false;
        try {
          await staging?.delete(recursive: true);
        } on FileSystemException {
          // The native picker may briefly retain the export source.
        }
      }
    }
  }

  /// Export an in-memory QR image through the same native destination picker.
  Future<bool> saveBytes({
    required Uint8List bytes,
    required String suggestedName,
    String mimeType = 'image/png',
  }) async {
    if (_saving) throw StateError('A native download is already in progress');
    _saving = true;
    Directory? staging;
    try {
      final directory = await getTemporaryDirectory();
      staging = await directory.createTemp('platform-qr-');
      final name = nativeDownloadFileName(suggestedName);
      final file = File('${staging.path}/$name');
      await file.writeAsBytes(bytes, flush: true);
      return await _channel.invokeMethod<bool>('saveFile', {
            'path': file.path,
            'name': name,
            'mimeType': mimeType,
          }) ??
          false;
    } finally {
      _saving = false;
      try {
        await staging?.delete(recursive: true);
      } on FileSystemException {
        // The system picker can briefly retain the export source.
      }
    }
  }

  /// Put a remote image or video into the photo album. Throws
  /// [AlbumAccessDenied] when the person refuses the photo permission, so the
  /// caller can fall back to the document picker instead of failing silently.
  Future<void> saveUrlToAlbum({
    required String url,
    required String suggestedName,
    String mimeType = 'application/octet-stream',
    void Function(double fraction)? onProgress,
  }) async {
    final video = mimeType.startsWith('video/');
    if (!video && !mimeType.startsWith('image/')) {
      throw ArgumentError.value(mimeType, 'mimeType', 'Not album media');
    }
    if (_saving) {
      throw StateError('A native download is already in progress');
    }
    final uri = Uri.tryParse(url);
    if (uri == null || !(uri.isScheme('https') || uri.isScheme('http'))) {
      throw ArgumentError.value(url, 'url', 'Expected an HTTP(S) download URL');
    }
    if (!await Gal.hasAccess(toAlbum: true) &&
        !await Gal.requestAccess(toAlbum: true)) {
      throw const AlbumAccessDenied();
    }
    _saving = true;
    Directory? staging;
    try {
      final directory = await getTemporaryDirectory();
      staging = Directory(
        '${directory.path}/album-${DateTime.now().microsecondsSinceEpoch}',
      );
      await staging.create(recursive: true);
      final file = File(
        '${staging.path}/${nativeDownloadFileName(suggestedName)}',
      );
      await _downloadClient.download(
        uri.toString(),
        file.path,
        deleteOnError: true,
        options: Options(responseType: ResponseType.bytes),
        onReceiveProgress: (received, total) {
          if (total > 0) onProgress?.call(received / total);
        },
      );
      try {
        if (video) {
          await Gal.putVideo(file.path);
        } else {
          await Gal.putImage(file.path);
        }
      } on GalException catch (error) {
        if (error.type == GalExceptionType.accessDenied) {
          throw const AlbumAccessDenied();
        }
        rethrow;
      }
    } finally {
      _saving = false;
      try {
        if (staging != null && staging.existsSync()) {
          staging.deleteSync(recursive: true);
        }
      } on FileSystemException {
        // Best effort; the OS may still be reading the import source.
      }
    }
  }

  /// Returns `true` after the system saved the file, or `false` when the user
  /// cancelled the native destination picker. Only one picker can be active at
  /// a time, matching the native platform contract.
  Future<bool> saveUrl({
    required String url,
    required String suggestedName,
    String mimeType = 'application/octet-stream',
    void Function(double fraction)? onProgress,
  }) async {
    if (_saving) {
      throw StateError('A native download is already in progress');
    }
    final uri = Uri.tryParse(url);
    if (uri == null || !(uri.isScheme('https') || uri.isScheme('http'))) {
      throw ArgumentError.value(url, 'url', 'Expected an HTTP(S) download URL');
    }

    _saving = true;
    File? temporaryFile;
    try {
      final directory = await getTemporaryDirectory();
      final safeName = nativeDownloadFileName(suggestedName);
      final staging = Directory(
        '${directory.path}/download-${DateTime.now().microsecondsSinceEpoch}',
      );
      await staging.create(recursive: true);
      temporaryFile = File('${staging.path}/$safeName');

      await _downloadClient.download(
        uri.toString(),
        temporaryFile.path,
        deleteOnError: true,
        options: Options(responseType: ResponseType.bytes),
        onReceiveProgress: (received, total) {
          if (total > 0) onProgress?.call(received / total);
        },
      );

      return await _channel.invokeMethod<bool>('saveFile', {
            'path': temporaryFile.path,
            'name': safeName,
            'mimeType': mimeType.trim().isEmpty
                ? 'application/octet-stream'
                : mimeType.trim(),
          }) ??
          false;
    } finally {
      _saving = false;
      final file = temporaryFile;
      if (file != null) {
        try {
          final parent = file.parent;
          if (parent.existsSync()) parent.deleteSync(recursive: true);
        } on FileSystemException {
          // Temporary cleanup is best effort; the OS may still briefly retain
          // the export source while dismissing its document picker.
        }
      }
    }
  }
}

/// The person declined photo-library access; callers offer the file picker.
class AlbumAccessDenied implements Exception {
  const AlbumAccessDenied();
}

/// Keeps the suggested name portable and prevents a remote path from escaping
/// the staging directory. The visible name remains recognisable to the user.
String nativeDownloadFileName(String value) {
  final base = value
      .replaceAll('\\', '/')
      .split('/')
      .last
      .replaceAll(RegExp(r'[\x00-\x1f\x7f/:*?"<>|]'), '_')
      .trim();
  if (base.isEmpty || base == '.' || base == '..') return 'download';
  return base.length <= 180 ? base : base.substring(base.length - 180);
}

final nativeDownloadProvider = Provider<NativeDownloadService>(
  (ref) => NativeDownloadService(),
);
