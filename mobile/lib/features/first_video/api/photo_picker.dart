import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

enum PhotoSource { camera, album }

class PickedPhoto {
  const PickedPhoto({
    required this.name,
    required this.mime,
    required this.bytes,
  });

  final String name;
  final String mime;
  final List<int> bytes;
}

/// Camera / album behind one seam so the flow can be driven in tests without
/// a platform picker. Null means the person backed out.
abstract class FirstVideoPhotoPicker {
  Future<PickedPhoto?> pick(PhotoSource source);
}

/// `image_picker`: the system camera or the photo picker. Long edges are
/// capped so a 12 MP shot does not go up as 12 MB — the video model reads
/// composition, not pixels.
class ImagePickerPhotoPicker implements FirstVideoPhotoPicker {
  ImagePickerPhotoPicker([ImagePicker? picker])
    : _picker = picker ?? ImagePicker();

  final ImagePicker _picker;

  @override
  Future<PickedPhoto?> pick(PhotoSource source) async {
    final file = await _picker.pickImage(
      source: switch (source) {
        PhotoSource.camera => ImageSource.camera,
        PhotoSource.album => ImageSource.gallery,
      },
      maxWidth: 2048,
      maxHeight: 2048,
      imageQuality: 88,
      requestFullMetadata: false,
    );
    if (file == null) return null;
    final bytes = await file.readAsBytes();
    final name = file.name.isEmpty ? 'photo.jpg' : file.name;
    return PickedPhoto(
      name: name,
      mime: file.mimeType ?? _mimeForName(name),
      bytes: bytes,
    );
  }

  static String _mimeForName(String name) {
    final lower = name.toLowerCase();
    if (lower.endsWith('.png')) return 'image/png';
    if (lower.endsWith('.heic') || lower.endsWith('.heif')) {
      return 'image/heic';
    }
    if (lower.endsWith('.webp')) return 'image/webp';
    return 'image/jpeg';
  }
}

final firstVideoPhotoPickerProvider = Provider<FirstVideoPhotoPicker>(
  (_) => ImagePickerPhotoPicker(),
);
