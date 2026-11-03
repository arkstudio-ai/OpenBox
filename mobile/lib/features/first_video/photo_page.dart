import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/toast.dart';
import 'api/photo_picker.dart';
import 'state/first_video_flow.dart';
import 'widgets/flow_chrome.dart';

/// ③ 拍一张: camera or album for one reference photo, or none at all. The
/// photo is uploaded and the job submitted from here; the making page only
/// ever watches.
class FirstVideoPhotoPage extends ConsumerStatefulWidget {
  const FirstVideoPhotoPage({super.key});

  @override
  ConsumerState<FirstVideoPhotoPage> createState() =>
      _FirstVideoPhotoPageState();
}

enum _Busy { none, picking, uploading, submitting }

class _FirstVideoPhotoPageState extends ConsumerState<FirstVideoPhotoPage> {
  _Busy _busy = _Busy.none;
  double _upload = 0;

  void _back() =>
      context.canPop() ? context.pop() : context.go(Paths.firstVideoTemplate);

  Future<void> _pick(PhotoSource source) async {
    if (_busy != _Busy.none) return;
    setState(() => _busy = _Busy.picking);
    try {
      final photo = await ref.read(firstVideoPhotoPickerProvider).pick(source);
      if (!mounted) return;
      if (photo != null) {
        ref.read(firstVideoDraftProvider.notifier).setPhoto(photo);
      }
    } catch (_) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .warning(ref.read(i18nProvider).t('first_video:photo.pickFailed'));
      }
    } finally {
      if (mounted) setState(() => _busy = _Busy.none);
    }
  }

  Future<void> _submit({required bool withPhoto}) async {
    if (_busy != _Busy.none) return;
    final hasPhoto = ref.read(firstVideoDraftProvider).photo != null;
    setState(() {
      _busy = withPhoto && hasPhoto ? _Busy.uploading : _Busy.submitting;
      _upload = 0;
    });
    try {
      await ref
          .read(firstVideoFlowProvider)
          .submit(
            withPhoto: withPhoto,
            onUploadProgress: (fraction) {
              if (!mounted) return;
              setState(() {
                _upload = fraction;
                if (fraction >= 1) _busy = _Busy.submitting;
              });
            },
          );
      if (mounted) context.go(Paths.firstVideoMaking);
    } catch (error) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(errorText(ref.read(i18nProvider), error));
      }
    } finally {
      if (mounted) setState(() => _busy = _Busy.none);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final draft = ref.watch(firstVideoDraftProvider);
    final photo = draft.photo;
    final idle = _busy == _Busy.none;
    final headline = draft.headline.trim();
    final title = headline.isEmpty
        ? i18n.t('first_video:photo.titlePlain')
        : i18n.t('first_video:photo.title', vars: {'headline': headline});

    return FirstVideoScaffold(
      step: 3,
      scrollable: false,
      leading: FirstVideoTopIcon(
        key: const Key('fv-back'),
        icon: Icons.arrow_back_ios_new,
        tooltip: i18n.t('first_video:back'),
        onTap: idle ? _back : null,
      ),
      trailing: FirstVideoTopAction(
        key: const Key('fv-skip'),
        label: i18n.t('first_video:skip'),
        onTap: idle ? () => _submit(withPhoto: false) : null,
      ),
      footer: photo == null
          ? _SourceRow(
              idle: idle,
              albumLabel: i18n.t('first_video:photo.album'),
              cameraTooltip: i18n.t('first_video:photo.camera'),
              textLabel: i18n.t('first_video:photo.textOnly'),
              onAlbum: () => _pick(PhotoSource.album),
              onCamera: () => _pick(PhotoSource.camera),
              onText: () => _submit(withPhoto: false),
            )
          : Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                FirstVideoPrimaryButton(
                  key: const Key('fv-use-photo'),
                  label: i18n.t('first_video:photo.use'),
                  onTap: idle ? () => _submit(withPhoto: true) : null,
                ),
                const SizedBox(height: 6),
                FirstVideoLinkButton(
                  key: const Key('fv-retake'),
                  label: i18n.t('first_video:photo.retake'),
                  onTap: idle
                      ? () => ref
                            .read(firstVideoDraftProvider.notifier)
                            .setPhoto(null)
                      : null,
                ),
              ],
            ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          FirstVideoHeading(
            title: title,
            lede: i18n.t('first_video:photo.lede'),
          ),
          const SizedBox(height: 16),
          Expanded(
            child: _Frame(
              photo: photo?.bytes,
              busy: _busy,
              uploadFraction: _upload,
              frameLabel: i18n.t('first_video:photo.frame'),
              frameHint: i18n.t('first_video:photo.frameHint'),
              busyLabel: switch (_busy) {
                _Busy.uploading => i18n.t('first_video:photo.uploading'),
                _Busy.submitting => i18n.t('first_video:photo.submitting'),
                _ => null,
              },
              tokens: t,
            ),
          ),
          const SizedBox(height: 8),
        ],
      ),
    );
  }
}

class _Frame extends StatelessWidget {
  const _Frame({
    required this.photo,
    required this.busy,
    required this.uploadFraction,
    required this.frameLabel,
    required this.frameHint,
    required this.busyLabel,
    required this.tokens,
  });

  final List<int>? photo;
  final _Busy busy;
  final double uploadFraction;
  final String frameLabel;
  final String frameHint;
  final String? busyLabel;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) {
    final t = tokens;
    final bytes = photo;
    return ClipRRect(
      borderRadius: BorderRadius.circular(Radii.xl2),
      child: Stack(
        fit: StackFit.expand,
        children: [
          ColoredBox(color: t.term),
          if (bytes != null)
            Image.memory(
              Uint8List.fromList(bytes),
              key: const Key('fv-photo-preview'),
              fit: BoxFit.cover,
              gaplessPlayback: true,
              errorBuilder: (_, _, _) => const SizedBox.shrink(),
            )
          else ...[
            Positioned(
              top: 16,
              left: 16,
              child: Text(
                frameLabel,
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  fontWeight: FontWeight.w600,
                  color: t.n300,
                ),
              ),
            ),
            Center(
              child: Container(
                width: 200,
                height: 200,
                decoration: BoxDecoration(
                  border: Border.all(color: t.n600, width: 2),
                  borderRadius: BorderRadius.circular(Radii.xl),
                ),
              ),
            ),
            Positioned(
              bottom: 16,
              left: 16,
              child: Text(
                frameHint,
                style: TextStyle(fontSize: FontSizes.sm, color: t.n400),
              ),
            ),
          ],
          if (busyLabel != null)
            ColoredBox(
              color: Colors.black54,
              child: Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    SizedBox(
                      width: 36,
                      height: 36,
                      child: CircularProgressIndicator(
                        strokeWidth: 3,
                        color: Colors.white,
                        value: busy == _Busy.uploading && uploadFraction > 0
                            ? uploadFraction
                            : null,
                      ),
                    ),
                    const SizedBox(height: 12),
                    Text(
                      busyLabel!,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: FontSizes.base,
                      ),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// 从相册选 · 拍照 · 不拍，用文字 — the camera's round shutter in the middle.
class _SourceRow extends StatelessWidget {
  const _SourceRow({
    required this.idle,
    required this.albumLabel,
    required this.cameraTooltip,
    required this.textLabel,
    required this.onAlbum,
    required this.onCamera,
    required this.onText,
  });

  final bool idle;
  final String albumLabel;
  final String cameraTooltip;
  final String textLabel;
  final VoidCallback onAlbum;
  final VoidCallback onCamera;
  final VoidCallback onText;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        _SideAction(
          key: const Key('fv-album'),
          icon: Icons.photo_library_outlined,
          label: albumLabel,
          onTap: idle ? onAlbum : null,
        ),
        Semantics(
          button: true,
          label: cameraTooltip,
          child: Material(
            key: const Key('fv-camera'),
            color: t.card,
            shape: CircleBorder(side: BorderSide(color: t.hair, width: 4)),
            child: InkWell(
              customBorder: const CircleBorder(),
              onTap: idle ? onCamera : null,
              child: Padding(
                padding: const EdgeInsets.all(6),
                child: Container(
                  width: 60,
                  height: 60,
                  decoration: BoxDecoration(
                    color: idle ? t.a700 : t.n300,
                    shape: BoxShape.circle,
                  ),
                  child: Icon(Icons.photo_camera_outlined, color: t.bg),
                ),
              ),
            ),
          ),
        ),
        _SideAction(
          key: const Key('fv-text-only'),
          icon: Icons.notes,
          label: textLabel,
          onTap: idle ? onText : null,
        ),
      ],
    );
  }
}

class _SideAction extends StatelessWidget {
  const _SideAction({
    super.key,
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return InkWell(
      borderRadius: BorderRadius.circular(Radii.md),
      onTap: onTap,
      child: SizedBox(
        width: 84,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 48,
              height: 48,
              decoration: BoxDecoration(
                color: t.surface,
                borderRadius: BorderRadius.circular(Radii.md),
              ),
              child: Icon(icon, size: 22, color: t.ink),
            ),
            const SizedBox(height: 6),
            Text(
              label,
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
            ),
          ],
        ),
      ),
    );
  }
}
