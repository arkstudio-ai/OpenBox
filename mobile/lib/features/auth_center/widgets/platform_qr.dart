import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:qr_flutter/qr_flutter.dart';

import '../../../shared/download/native_download.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/platforms/platform_links.dart';
import '../../../shared/widgets/toast.dart';

/// QR black/white is intentional machine-readable content, independent of theme.
class PlatformQr extends ConsumerStatefulWidget {
  const PlatformQr({
    super.key,
    required this.uri,
    required this.valid,
    required this.name,
    this.authorize = false,
  });
  final Uri uri;
  final bool valid;
  final String name;
  final bool authorize;
  @override
  ConsumerState<PlatformQr> createState() => _PlatformQrState();
}

class _PlatformQrState extends ConsumerState<PlatformQr> {
  bool _busy = false;

  Future<void> _open() async {
    if (_busy || !widget.valid) return;
    setState(() => _busy = true);
    final launcher = ref.read(platformLinkLauncherProvider);
    try {
      final opened = await launcher.open(widget.uri);
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .info(
              ref
                  .read(i18nProvider)
                  .t(
                    opened
                        ? (widget.authorize
                              ? 'auth-center:mobile.authorizeReturnHint'
                              : 'auth-center:mobile.returnHint')
                        : 'auth-center:mobile.launchFailed',
                  ),
            );
      }
    } catch (_) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(ref.read(i18nProvider).t('auth-center:mobile.launchFailed'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _save() async {
    if (_busy || !widget.valid) return;
    setState(() => _busy = true);
    final downloads = ref.read(nativeDownloadProvider);
    try {
      final raster = await platformQrImage(widget.uri.toString());
      final image = await raster.toByteData(format: ui.ImageByteFormat.png);
      raster.dispose();
      if (image == null || !mounted || !widget.valid) return;
      final saved = await downloads.saveBytes(
        bytes: image.buffer.asUint8List(),
        suggestedName: widget.name,
      );
      if (mounted && saved) {
        ref
            .read(toastProvider.notifier)
            .success(ref.read(i18nProvider).t('auth-center:mobile.saved'));
      }
    } catch (_) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(ref.read(i18nProvider).t('auth-center:mobile.saveFailed'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    if (!widget.valid) return Text(i18n.t('auth-center:mobile.linkExpired'));
    return Column(
      children: [
        FilledButton.icon(
          onPressed: _busy ? null : _open,
          icon: const Icon(Icons.open_in_new, size: 18),
          label: Text(
            i18n.t(
              widget.authorize
                  ? 'auth-center:mobile.openAuthorize'
                  : 'auth-center:mobile.openDouyin',
            ),
          ),
        ),
        const SizedBox(height: 8),
        Text(
          i18n.t(
            widget.authorize
                ? 'auth-center:mobile.authorizeReturnHint'
                : 'auth-center:mobile.returnHint',
          ),
          textAlign: TextAlign.center,
        ),
        const SizedBox(height: 12),
        QrImageView(
          data: widget.uri.toString(),
          size: 240,
          padding: const EdgeInsets.all(16),
          backgroundColor: Colors.white,
          errorCorrectionLevel: QrErrorCorrectLevel.M,
          semanticsLabel: i18n.t(
            widget.authorize
                ? 'auth-center:mobile.authorizeQrAlt'
                : 'auth-center:publish.qrAlt',
          ),
        ),
        TextButton.icon(
          onPressed: _busy ? null : _save,
          icon: const Icon(Icons.download_outlined, size: 18),
          label: Text(i18n.t('auth-center:mobile.saveQr')),
        ),
        Text(
          i18n.t('auth-center:mobile.qrFallback'),
          textAlign: TextAlign.center,
        ),
      ],
    );
  }
}

/// Keep a solid white quiet zone in the exported PNG too, not just the widget.
Future<ui.Image> platformQrImage(String data) async {
  final recorder = ui.PictureRecorder();
  final canvas = Canvas(recorder);
  canvas.drawRect(
    const Rect.fromLTWH(0, 0, 1024, 1024),
    Paint()..color = Colors.white,
  );
  canvas.translate(64, 64);
  QrPainter(
    data: data,
    version: QrVersions.auto,
    errorCorrectionLevel: QrErrorCorrectLevel.M,
    gapless: true,
  ).paint(canvas, const Size(896, 896));
  final picture = recorder.endRecording();
  try {
    return await picture.toImage(1024, 1024);
  } finally {
    picture.dispose();
  }
}
