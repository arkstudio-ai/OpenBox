import 'package:flutter/material.dart';

import '../../../shared/i18n/i18n.dart';
import '../../chat/utils/task_status.dart';

String lightActivityDate(DateTime date, I18nState i18n) {
  final local = date.toLocal();
  final now = DateTime.now();
  final time =
      '${local.hour.toString().padLeft(2, '0')}:${local.minute.toString().padLeft(2, '0')}';
  if (DateUtils.isSameDay(local, now)) {
    return i18n.t('light:activity.today', vars: {'time': time});
  }
  final yesterday = DateTime(now.year, now.month, now.day - 1);
  if (DateUtils.isSameDay(local, yesterday)) {
    return i18n.t('light:activity.yesterday', vars: {'time': time});
  }
  return shortDate(local, i18n.language, year: local.year != now.year);
}
