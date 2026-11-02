import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../../chat/chat_screen.dart' show ChatInputState;
import '../api/light_api.dart';

/// Picks photos for the topic chat; a seam so tests can drive the bar
/// without a platform picker.
abstract class LightPhotoPicker {
  Future<List<XFile>> pick({required bool camera, required int limit});

  /// Photos and videos from the album (你拍我剪). Videos are not re-encoded.
  Future<List<XFile>> pickMedia({required int limit});
}

class ImagePickerLightPhotoPicker implements LightPhotoPicker {
  ImagePickerLightPhotoPicker([ImagePicker? picker])
    : _picker = picker ?? ImagePicker();

  final ImagePicker _picker;

  @override
  Future<List<XFile>> pick({required bool camera, required int limit}) async {
    if (camera) {
      final one = await _picker.pickImage(
        source: ImageSource.camera,
        maxWidth: 2048,
        maxHeight: 2048,
        imageQuality: 88,
        requestFullMetadata: false,
      );
      return one == null ? const [] : [one];
    }
    final many = await _picker.pickMultiImage(
      maxWidth: 2048,
      maxHeight: 2048,
      imageQuality: 88,
      limit: limit > 1 ? limit : null,
      requestFullMetadata: false,
    );
    return many.take(limit).toList();
  }

  @override
  Future<List<XFile>> pickMedia({required int limit}) async {
    final many = await _picker.pickMultipleMedia(
      imageQuality: 88,
      limit: limit > 1 ? limit : null,
      requestFullMetadata: false,
    );
    return many.take(limit).toList();
  }
}

/// A clip larger than this is refused before upload (plan §5.1: ≤ 200 MB each).
const lightMaxClipBytes = 200 * 1024 * 1024;

bool lightIsVideo(XFile file) {
  final mime = file.mimeType ?? '';
  if (mime.startsWith('video/')) return true;
  final lower = file.name.toLowerCase();
  return lower.endsWith('.mov') ||
      lower.endsWith('.mp4') ||
      lower.endsWith('.m4v');
}

final lightPhotoPickerProvider = Provider<LightPhotoPicker>(
  (_) => ImagePickerLightPhotoPicker(),
);

/// The context input bar of a topic chat (docs `LIGHT_MODE_PLAN.md` §4.4):
/// the step's quick chips, a text box, camera and album. Chips show while
/// the conversation is still at its opening card; after that the cards in
/// the transcript carry the choices. Photos upload on send and ride along
/// as attachments, with a stock line when nothing was typed.
///
/// Replaces the [Composer] through [ChatScreen.inputBuilder]; it never
/// reaches into the chat stores itself.
class LightInputBar extends ConsumerStatefulWidget {
  const LightInputBar({
    super.key,
    required this.state,
    this.chips = const [],
    this.maxPhotos = 3,
    this.allowVideo = false,
  });

  final ChatInputState state;
  final List<String> chips;
  final int maxPhotos;

  /// 你拍我剪: the album picks videos too, and clips upload one by one with
  /// their own progress.
  final bool allowVideo;

  @override
  ConsumerState<LightInputBar> createState() => _LightInputBarState();
}

class _LightInputBarState extends ConsumerState<LightInputBar> {
  final _controller = TextEditingController();
  final _photos = <XFile>[];
  bool _sending = false;
  double? _progress;
  int _uploadingIndex = 0;

  @override
  void initState() {
    super.initState();
    _controller.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  bool get _busy => _sending || widget.state.busy;

  bool get _showChips =>
      widget.chips.isNotEmpty &&
      widget.state.userTurns <= 1 &&
      !widget.state.busy &&
      !widget.state.hasPendingInput &&
      _controller.text.trim().isEmpty &&
      _photos.isEmpty;

  bool get _canSend =>
      !_sending &&
      !widget.state.busy &&
      (_controller.text.trim().isNotEmpty || _photos.isNotEmpty);

  Future<void> _pick({required bool camera}) async {
    if (_busy || _photos.length >= widget.maxPhotos) return;
    try {
      final picker = ref.read(lightPhotoPickerProvider);
      final room = widget.maxPhotos - _photos.length;
      var picked = widget.allowVideo && !camera
          ? await picker.pickMedia(limit: room)
          : await picker.pick(camera: camera, limit: room);
      if (!mounted || picked.isEmpty) return;
      final sizes = await Future.wait(picked.map((f) => f.length()));
      final tooBig = [
        for (final (index, file) in picked.indexed)
          if (lightIsVideo(file) && sizes[index] > lightMaxClipBytes) file,
      ];
      if (tooBig.isNotEmpty) {
        picked = picked.where((f) => !tooBig.contains(f)).toList();
        ref
            .read(toastProvider.notifier)
            .info(ref.read(i18nProvider).t('light:topic.input.clipTooBig'));
      }
      if (!mounted || picked.isEmpty) return;
      setState(() => _photos.addAll(picked));
    } catch (error) {
      if (!mounted) return;
      ref
          .read(toastProvider.notifier)
          .error(errorText(ref.read(i18nProvider), error));
    }
  }

  Future<void> _send([String? chip]) async {
    final text = (chip ?? _controller.text).trim();
    if (text.isEmpty && _photos.isEmpty) return;
    if (_sending || widget.state.busy) return;
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    setState(() => _sending = true);
    try {
      final ids = <String>[];
      // The transport is only needed when photos ride along.
      final api = _photos.isEmpty ? null : ref.read(lightApiProvider);
      for (final (index, photo) in _photos.indexed) {
        void progress(double fraction) {
          if (mounted) {
            setState(() {
              _progress = (index + fraction) / _photos.length;
              _uploadingIndex = index;
            });
          }
        }

        final video = lightIsVideo(photo);
        final name = photo.name.isEmpty
            ? (video ? 'clip-${index + 1}.mov' : 'photo-${index + 1}.jpg')
            : photo.name;
        final mime = photo.mimeType ?? _mimeForName(photo.name);
        ids.add(
          video
              ? await api!.uploadStream(
                  name: name,
                  mime: mime,
                  length: await photo.length(),
                  stream: photo.openRead,
                  onProgress: progress,
                )
              : await api!.uploadImage(
                  name: name,
                  mime: mime,
                  bytes: await photo.readAsBytes(),
                  onProgress: progress,
                ),
        );
      }
      final clips = _photos.any(lightIsVideo);
      await widget.state.onSend(
        text.isNotEmpty
            ? text
            : i18n.t(
                clips
                    ? 'light:topic.input.clipsSent'
                    : 'light:topic.input.photosSent',
              ),
        ids,
      );
      if (!mounted) return;
      _controller.clear();
      setState(_photos.clear);
    } catch (error) {
      if (!mounted) return;
      toast.error(errorText(i18n, error));
    } finally {
      if (mounted) {
        setState(() {
          _sending = false;
          _progress = null;
        });
      }
    }
  }

  static String _mimeForName(String name) {
    final lower = name.toLowerCase();
    if (lower.endsWith('.mov')) return 'video/quicktime';
    if (lower.endsWith('.mp4')) return 'video/mp4';
    if (lower.endsWith('.m4v')) return 'video/x-m4v';
    if (lower.endsWith('.png')) return 'image/png';
    if (lower.endsWith('.heic') || lower.endsWith('.heif')) {
      return 'image/heic';
    }
    if (lower.endsWith('.webp')) return 'image/webp';
    return 'image/jpeg';
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final state = widget.state;
    return Container(
      key: const Key('light-input-bar'),
      decoration: BoxDecoration(
        color: t.card,
        border: Border(top: BorderSide(color: t.hair)),
      ),
      padding: const EdgeInsets.fromLTRB(8, 8, 8, 8),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_showChips)
            SizedBox(
              height: 40,
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.symmetric(horizontal: 4),
                itemCount: widget.chips.length,
                separatorBuilder: (_, _) => const SizedBox(width: 8),
                itemBuilder: (context, index) {
                  final chip = widget.chips[index];
                  return ActionChip(
                    key: Key('light-chip-$index'),
                    label: Text(
                      chip,
                      style: TextStyle(
                        fontSize: FontSizes.sm,
                        fontWeight: FontWeight.w600,
                        color: t.ink,
                      ),
                    ),
                    backgroundColor: t.a100,
                    side: BorderSide(color: t.a300),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                    onPressed: () => _send(chip),
                  );
                },
              ),
            ),
          if (_photos.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(4, 8, 4, 2),
              child: SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                clipBehavior: Clip.none,
                child: Row(
                  children: [
                    for (final (index, photo) in _photos.indexed) ...[
                      if (index > 0) const SizedBox(width: 8),
                      _PhotoThumb(
                        photo: photo,
                        onRemove: _sending
                            ? null
                            : () => setState(() => _photos.removeAt(index)),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          if (_progress != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(4, 6, 4, 0),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  LinearProgressIndicator(
                    value: _progress,
                    minHeight: 3,
                    backgroundColor: t.hairSoft,
                  ),
                  const SizedBox(height: 4),
                  Text(
                    _photos.length > 1
                        ? i18n.t(
                            'light:topic.input.uploadingOf',
                            vars: {
                              'n': _uploadingIndex + 1,
                              'total': _photos.length,
                            },
                          )
                        : i18n.t('light:topic.input.uploading'),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ],
              ),
            ),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              IconButton(
                key: const Key('light-camera'),
                tooltip: i18n.t('light:topic.input.camera'),
                onPressed: _busy || _photos.length >= widget.maxPhotos
                    ? null
                    : () => _pick(camera: true),
                icon: Icon(Icons.photo_camera_outlined, color: t.n700),
              ),
              IconButton(
                key: const Key('light-album'),
                tooltip: i18n.t('light:topic.input.album'),
                onPressed: _busy || _photos.length >= widget.maxPhotos
                    ? null
                    : () => _pick(camera: false),
                icon: Icon(Icons.photo_library_outlined, color: t.n700),
              ),
              Expanded(
                child: TextField(
                  key: const Key('light-input'),
                  controller: _controller,
                  enabled: !_sending,
                  minLines: 1,
                  maxLines: 4,
                  textInputAction: TextInputAction.newline,
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                  decoration: InputDecoration(
                    isDense: true,
                    hintText: i18n.t('light:topic.input.hint'),
                    hintStyle: TextStyle(
                      fontSize: FontSizes.base,
                      color: t.n500,
                    ),
                    filled: true,
                    fillColor: t.bg,
                    contentPadding: const EdgeInsets.symmetric(
                      horizontal: 14,
                      vertical: 10,
                    ),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(Radii.xl),
                      borderSide: BorderSide(color: t.hair),
                    ),
                    enabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(Radii.xl),
                      borderSide: BorderSide(color: t.hair),
                    ),
                    focusedBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(Radii.xl),
                      borderSide: BorderSide(color: t.accent),
                    ),
                    disabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(Radii.xl),
                      borderSide: BorderSide(color: t.hair),
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 6),
              if (state.busy && state.onStop != null)
                IconButton.filledTonal(
                  key: const Key('light-stop'),
                  tooltip: i18n.t('light:topic.input.stop'),
                  onPressed: state.onStop,
                  icon: const Icon(Icons.stop_rounded),
                )
              else
                IconButton.filled(
                  key: const Key('light-send'),
                  tooltip: i18n.t('light:topic.input.send'),
                  onPressed: _canSend ? _send : null,
                  style: IconButton.styleFrom(
                    backgroundColor: t.ink,
                    foregroundColor: t.bg,
                    disabledBackgroundColor: t.n300,
                    disabledForegroundColor: t.n600,
                  ),
                  icon: _sending
                      ? SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: t.bg,
                          ),
                        )
                      : const Icon(Icons.arrow_upward),
                ),
            ],
          ),
        ],
      ),
    );
  }
}

class _PhotoThumb extends StatelessWidget {
  const _PhotoThumb({required this.photo, required this.onRemove});

  final XFile photo;
  final VoidCallback? onRemove;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Stack(
      clipBehavior: Clip.none,
      children: [
        ClipRRect(
          borderRadius: BorderRadius.circular(Radii.md),
          child: SizedBox(
            width: 56,
            height: 56,
            child: lightIsVideo(photo)
                // A clip is not decoded here (it may be hundreds of MB).
                ? ColoredBox(
                    color: t.n800,
                    child: Icon(
                      Icons.videocam_outlined,
                      color: t.mediaLabel,
                      size: 24,
                    ),
                  )
                : FutureBuilder<Uint8List>(
                    future: photo.readAsBytes(),
                    builder: (context, snapshot) => snapshot.hasData
                        ? Image.memory(snapshot.data!, fit: BoxFit.cover)
                        : ColoredBox(color: t.n300),
                  ),
          ),
        ),
        if (onRemove != null)
          Positioned(
            top: -6,
            right: -6,
            child: GestureDetector(
              onTap: onRemove,
              child: Container(
                width: 20,
                height: 20,
                decoration: BoxDecoration(color: t.ink, shape: BoxShape.circle),
                child: Icon(Icons.close, size: 12, color: t.bg),
              ),
            ),
          ),
      ],
    );
  }
}
