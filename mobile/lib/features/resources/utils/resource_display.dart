import 'package:flutter/material.dart';

import '../../../shared/models/resource.dart';

/// Value → icon / i18n-key tables (web `features/resources/constants`).
/// Explicit maps rather than built keys, so every key stays greppable.

const kindFilters = ['all', ...resourceKinds];

const sourceFilters = ['all', 'user', 'agent'];

const sortOptions = ['created', 'name', 'size'];

const _kindIcons = <String, IconData>{
  'image': Icons.image_outlined,
  'video': Icons.movie_outlined,
  'audio': Icons.audiotrack_outlined,
  'document': Icons.description_outlined,
  'archive': Icons.archive_outlined,
  'code': Icons.code,
  'other': Icons.insert_drive_file_outlined,
};

IconData kindIcon(String kind) =>
    _kindIcons[kind] ?? Icons.insert_drive_file_outlined;

const _kindLabelKeys = <String, String>{
  'all': 'resources:kind.all',
  'image': 'resources:kind.image',
  'video': 'resources:kind.video',
  'audio': 'resources:kind.audio',
  'document': 'resources:kind.document',
  'archive': 'resources:kind.archive',
  'code': 'resources:kind.code',
  'other': 'resources:kind.other',
};

String kindLabelKey(String kind) =>
    _kindLabelKeys[kind] ?? 'resources:kind.other';

const _sourceLabelKeys = <String, String>{
  'all': 'resources:source.all',
  'user': 'resources:source.user',
  'agent': 'resources:source.agent',
};

String sourceLabelKey(String source) =>
    _sourceLabelKeys[source] ?? 'resources:source.all';

IconData sourceIcon(String source) => switch (source) {
      'user' => Icons.person_outline,
      'agent' => Icons.smart_toy_outlined,
      _ => Icons.layers_outlined,
    };

const _sortLabelKeys = <String, String>{
  'created': 'resources:sort.created',
  'name': 'resources:sort.name',
  'size': 'resources:sort.size',
};

String sortLabelKey(String sort) =>
    _sortLabelKeys[sort] ?? 'resources:sort.created';

/// Which resources the preview pane can render as text — mirrors the web
/// `isTextPreviewable`: code always, documents only when they really are text.
bool isTextPreviewable(Resource resource) {
  if (resource.kind == 'code') return true;
  if (resource.kind != 'document') return false;
  final mime = resource.mime.toLowerCase();
  if (mime.startsWith('text/') ||
      mime == 'application/json' ||
      mime == 'application/xml') {
    return true;
  }
  return RegExp(r'\.(md|txt|csv|log)$', caseSensitive: false)
      .hasMatch(resource.name);
}

bool isPdf(Resource resource) =>
    resource.mime == 'application/pdf' ||
    resource.name.toLowerCase().endsWith('.pdf');

/// Mime for an upload picked off the device, when the picker offers none.
String mimeForName(String name) {
  final ext = name.contains('.') ? name.split('.').last.toLowerCase() : '';
  return switch (ext) {
    'png' => 'image/png',
    'jpg' || 'jpeg' => 'image/jpeg',
    'gif' => 'image/gif',
    'webp' => 'image/webp',
    'heic' => 'image/heic',
    'mp4' || 'm4v' => 'video/mp4',
    'mov' => 'video/quicktime',
    'mp3' => 'audio/mpeg',
    'm4a' => 'audio/mp4',
    'wav' => 'audio/wav',
    'pdf' => 'application/pdf',
    'txt' || 'log' => 'text/plain',
    'md' => 'text/markdown',
    'csv' => 'text/csv',
    'json' => 'application/json',
    'zip' => 'application/zip',
    _ => 'application/octet-stream',
  };
}
