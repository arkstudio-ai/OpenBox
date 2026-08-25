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

String _trim(double v) {
  final s = v.toStringAsFixed(1);
  return s.endsWith('.0') ? s.substring(0, s.length - 2) : s;
}
