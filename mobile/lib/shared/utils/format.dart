/// Number/duration formatters, mirroring frontend-v2 `shared/utils/format.ts`.
library;

String formatTokens(num tokens) {
  if (tokens >= 1000000) {
    return '${_trim(tokens / 1000000)}M';
  }
  if (tokens >= 1000) {
    return '${_trim(tokens / 1000)}k';
  }
  return tokens.round().toString();
}

String formatCost(num cost) => '\$${cost.toStringAsFixed(cost < 0.1 ? 4 : 2)}';

/// Seconds → "4.2s" / "1m 23s" / "1h 2m".
String formatDuration(num seconds) {
  if (seconds < 60) {
    return seconds < 10 ? '${seconds.toStringAsFixed(1)}s' : '${seconds.round()}s';
  }
  final total = seconds.round();
  final h = total ~/ 3600;
  final m = (total % 3600) ~/ 60;
  final s = total % 60;
  if (h > 0) return '${h}h ${m}m';
  return s > 0 ? '${m}m ${s}s' : '${m}m';
}

/// Localized relative time (web `formatRelative` via Intl.RelativeTimeFormat;
/// Dart has no built-in, so the two supported languages are spelled out).
String formatRelative(DateTime target, String language, {DateTime? now}) {
  final diff = target.difference(now ?? DateTime.now());
  final seconds = diff.inSeconds;
  final abs = seconds.abs();
  final zh = language.startsWith('zh');

  final (value, zhUnit, enUnit) = abs < 60
      ? (abs, '秒', 'second')
      : abs < 3600
          ? ((abs / 60).round(), '分钟', 'minute')
          : abs < 86400
              ? ((abs / 3600).round(), '小时', 'hour')
              : ((abs / 86400).round(), '天', 'day');

  final enPlural = value == 1 ? enUnit : '${enUnit}s';
  if (seconds >= 0) {
    return zh ? '$value$zhUnit后' : 'in $value $enPlural';
  }
  return zh ? '$value$zhUnit前' : '$value $enPlural ago';
}

/// Bytes → "1.2 MB" (web `formatBytes`).
String formatBytes(num bytes) {
  if (bytes < 1024) return '$bytes B';
  if (bytes < 1024 * 1024) return '${_trim(bytes / 1024)} KB';
  if (bytes < 1024 * 1024 * 1024) return '${_trim(bytes / (1024 * 1024))} MB';
  return '${_trim(bytes / (1024 * 1024 * 1024))} GB';
}

String _trim(double v) {
  final s = v.toStringAsFixed(1);
  return s.endsWith('.0') ? s.substring(0, s.length - 2) : s;
}
