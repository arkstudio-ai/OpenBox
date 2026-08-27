import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../../../shared/widgets/fold.dart';

/// Full evidence for video script/segment approvals (web
/// `VideoApprovalDetail`). Unknown question details intentionally render
/// nothing, so stored requests written before this existed stay compatible.
class VideoApprovalDetail extends ConsumerWidget {
  const VideoApprovalDetail({super.key, required this.item});

  final QuestionItem item;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final detail = item.detail;
    if (detail == null) return const SizedBox.shrink();

    final kind = _firstText(detail, const ['kind', 'type']);
    final script = _readScript(detail);
    final segments = _readSegments(detail);
    final isVideoDetail = kind == 'video_script_approval' ||
        kind == 'video_segments_approval' ||
        detail.containsKey('script_text') ||
        detail['segments'] is List;
    if (!isVideoDetail) return const SizedBox.shrink();

    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.all(11),
      constraints: BoxConstraints(
        maxHeight: MediaQuery.sizeOf(context).height * 0.42,
      ),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
        color: t.bg,
      ),
      child: SingleChildScrollView(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (script != null) ...[
              Text(
                i18n.t('chat:question.videoApproval.fullScript'),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  fontWeight: FontWeight.w500,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 5),
              Text(
                script,
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  height: 1.65,
                  color: t.n700,
                ),
              ),
            ],
            for (final segment in segments) _SegmentCard(segment: segment),
          ],
        ),
      ),
    );
  }
}

class _VideoSegmentDetail {
  const _VideoSegmentDetail({
    required this.ordinal,
    required this.role,
    required this.scriptText,
    required this.prompt,
  });

  final int ordinal;
  final String role;
  final String scriptText;
  final String prompt;
}

class _SegmentCard extends ConsumerStatefulWidget {
  const _SegmentCard({required this.segment});

  final _VideoSegmentDetail segment;

  @override
  ConsumerState<_SegmentCard> createState() => _SegmentCardState();
}

class _SegmentCardState extends ConsumerState<_SegmentCard> {
  bool _promptOpen = false;

  static const _roles = ['hook', 'body', 'transition', 'closing'];

  String _roleLabel(I18nState i18n, String role) {
    if (_roles.contains(role)) {
      return i18n.t('chat:question.videoApproval.roles.$role');
    }
    return role.isNotEmpty
        ? role
        : i18n.t('chat:question.videoApproval.roles.unknown');
  }

  String _promptSummary(String prompt) {
    final compact = prompt.replaceAll(RegExp(r'\s+'), ' ').trim();
    return compact.length > 60 ? '${compact.substring(0, 60)}…' : compact;
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final segment = widget.segment;
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.all(11),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
        color: t.card,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              Text(
                i18n.t('chat:question.videoApproval.segment',
                    vars: {'number': segment.ordinal}),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  fontWeight: FontWeight.w500,
                  color: t.ink,
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
                decoration: BoxDecoration(
                  color: t.hairSoft,
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
                child: Text(
                  i18n.t('chat:question.videoApproval.role',
                      vars: {'role': _roleLabel(i18n, segment.role)}),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                ),
              ),
            ],
          ),
          if (segment.scriptText.isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              i18n.t('chat:question.videoApproval.transcript'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
            const SizedBox(height: 3),
            Text(
              segment.scriptText,
              style: TextStyle(
                fontSize: FontSizes.sm,
                height: 1.65,
                color: t.ink,
              ),
            ),
          ],
          if (segment.prompt.isNotEmpty) ...[
            const SizedBox(height: 8),
            Divider(height: 1, color: t.hair),
            GestureDetector(
              onTap: () => setState(() => _promptOpen = !_promptOpen),
              child: Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Row(
                  children: [
                    AnimatedRotation(
                      turns: _promptOpen ? 0.5 : 0,
                      duration: const Duration(milliseconds: 150),
                      child: Icon(Icons.expand_more, size: 14, color: t.n700),
                    ),
                    const SizedBox(width: 3),
                    Expanded(
                      child: Text(
                        '${i18n.t('chat:question.videoApproval.prompt')}: '
                        '${_promptSummary(segment.prompt)}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                      ),
                    ),
                  ],
                ),
              ),
            ),
            Fold(
              open: _promptOpen,
              child: Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  segment.prompt,
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    height: 1.6,
                    color: t.n700,
                  ),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

Map<String, dynamic>? _asRecord(Object? value) =>
    value is Map<String, dynamic> ? value : null;

String? _firstText(Map<String, dynamic>? record, List<String> keys) {
  if (record == null) return null;
  for (final key in keys) {
    final value = record[key];
    if (value is String && value.trim().isNotEmpty) return value;
  }
  return null;
}

String? _readScript(Map<String, dynamic> detail) {
  final direct =
      _firstText(detail, const ['script_text', 'scriptText', 'content']);
  if (direct != null) return direct;
  return _firstText(
    _asRecord(detail['script']),
    const ['text', 'script_text', 'content'],
  );
}

List<_VideoSegmentDetail> _readSegments(Map<String, dynamic> detail) {
  final raw = detail['segments'];
  if (raw is! List) return const [];
  final out = <_VideoSegmentDetail>[];
  for (final (index, value) in raw.indexed) {
    final segment = _asRecord(value);
    if (segment == null) continue;
    final rawOrdinal =
        segment['ordinal'] ?? segment['index'] ?? segment['number'];
    final ordinal = rawOrdinal is num && rawOrdinal.isFinite
        ? rawOrdinal.toInt()
        : (rawOrdinal is String ? int.tryParse(rawOrdinal) : null) ?? index + 1;
    final scriptText = _firstText(segment, const [
          'script_text',
          'scriptText',
          'transcript',
          'dialogue',
          'content',
        ]) ??
        '';
    final prompt = _firstText(segment,
            const ['prompt', 'segment_prompt', 'segmentPrompt']) ??
        '';
    if (scriptText.isEmpty && prompt.isEmpty) continue;
    out.add(_VideoSegmentDetail(
      ordinal: ordinal,
      role: _firstText(segment, const ['role', 'speaker']) ?? '',
      scriptText: scriptText,
      prompt: prompt,
    ));
  }
  return out;
}
