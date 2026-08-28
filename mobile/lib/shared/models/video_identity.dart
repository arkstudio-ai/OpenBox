/// Real-person authorisation records, mirroring frontend-v2
/// `features/chat/api/video-identities.ts`. A video production that puts a
/// real presenter on screen needs that person's own liveness authorisation
/// before anything is generated; these are the records of it.
library;

import 'json.dart';

/// awaiting_user | active | expired | failed
const videoIdentityStatuses = ['awaiting_user', 'active', 'expired', 'failed'];

class VideoIdentity {
  const VideoIdentity({
    required this.identityId,
    required this.label,
    required this.status,
    this.provider = '',
    this.providerGroupId,
    this.authorizationUrl,
    this.qrCode,
    this.expiresAt,
    this.authorizedAt,
    this.createdAt,
    this.updatedAt,
    this.error,
  });

  final String identityId;
  final String label;
  final String provider;
  final String status;
  final String? providerGroupId;

  /// The page the person opens to authorise. Only ever https — anything else
  /// is dropped rather than launched.
  final String? authorizationUrl;

  /// A `data:image/…` payload, so it renders without a network round trip.
  final String? qrCode;
  final String? expiresAt;
  final String? authorizedAt;
  final String? createdAt;
  final String? updatedAt;
  final String? error;

  bool get isPending => status == 'awaiting_user';

  bool get isActive => status == 'active';

  bool get isTerminal => status == 'failed' || status == 'expired';

  /// Parse one record, or null when it is not a usable identity. An expired
  /// deadline downgrades `awaiting_user` to `expired` here rather than
  /// leaving a dead link on screen.
  static VideoIdentity? tryParse(Object? value) {
    if (value is! Map<String, dynamic>) return null;
    final id = asString(value['identity_id']) ?? '';
    var status = asString(value['status']) ?? '';
    if (id.isEmpty || !videoIdentityStatuses.contains(status)) return null;

    final expiresAt = asString(value['expires_at']);
    if (status == 'awaiting_user' && expiresAt != null) {
      final deadline = DateTime.tryParse(expiresAt);
      if (deadline != null && !deadline.isAfter(DateTime.now())) {
        status = 'expired';
      }
    }
    final expired = status == 'expired';
    final label = asString(value['label']);
    return VideoIdentity(
      identityId: id,
      label: (label != null && label.isNotEmpty) ? label : '真人主持人',
      provider: asString(value['provider']) ?? '',
      status: status,
      providerGroupId: asString(value['provider_group_id']),
      authorizationUrl: expired ? null : asString(value['authorization_url']),
      qrCode: expired ? null : asString(value['qr_code']),
      expiresAt: expiresAt,
      authorizedAt: asString(value['authorized_at']),
      createdAt: asString(value['created_at']),
      updatedAt: asString(value['updated_at']),
      error: asString(value['error']),
    );
  }
}

/// A reference image or clip filed under one identity.
class VideoMaterialAsset {
  const VideoMaterialAsset({
    required this.materialAssetId,
    required this.identityId,
    required this.status,
    this.assetType = 'Image',
    this.error,
  });

  final String materialAssetId;
  final String identityId;

  /// processing | active | failed
  final String status;

  /// Image | Video
  final String assetType;
  final String? error;

  bool get isActive => status == 'active';

  static VideoMaterialAsset? tryParse(Object? value) {
    if (value is! Map<String, dynamic>) return null;
    final id = asString(value['material_asset_id']) ?? '';
    final status = asString(value['status']) ?? '';
    if (id.isEmpty || !['processing', 'active', 'failed'].contains(status)) {
      return null;
    }
    return VideoMaterialAsset(
      materialAssetId: id,
      identityId: asString(value['identity_id']) ?? '',
      status: status,
      assetType: asString(value['asset_type']) == 'Video' ? 'Video' : 'Image',
      error: asString(value['error']),
    );
  }
}
