/// Older personal-assistant answers were written before it learned to keep
/// internal identifiers to itself ("（ID: 01M4…）", "（outcome: succeeded）").
/// The saved history stays as it is; the screen just does not show those
/// bits (web `hideInternalIds`). Only identifier-shaped tokens and known
/// internal field names are touched.
library;

const _id =
    r'(?:[0-9A-HJKMNP-TV-Z]{26}|(?:session|task|result|memory|mem|msg|message|run|inbox|command|request|briefing|part|cmd)_[0-9A-Za-z]{12,})';
const _field =
    r'(?:outcome|enabled|status|state|observed_state|desired_state|delivery_state|task_id|session_id|result_id|run_id|message_id|request_id|inbox_id|command_id|revision|generation|report_attempt)';

final _labelledId = RegExp(
  '\\s*[（(]\\s*(?:[\\u4e00-\\u9fa5A-Za-z ]{0,8}\\s*)?(?:ID|id|编号)\\s*[:：]\\s*`?$_id`?\\s*[）)]',
);
final _fieldValue = RegExp(
  '\\s*[（(]\\s*`?$_field\\s*[:：=]\\s*[A-Za-z0-9_.\\-]+`?\\s*[）)]',
);
final _codeId = RegExp('\\s*`$_id`');
final _bareLabelledId = RegExp(
  '[，,]?\\s*(?:[\\u4e00-\\u9fa5]{0,4}|[A-Za-z]{0,8}\\s?)(?:ID|编号)\\s*[:：]\\s*$_id',
);

String hideInternalIds(String text) {
  if (text.isEmpty) return text;
  return text
      .replaceAll(_labelledId, '')
      .replaceAll(_fieldValue, '')
      .replaceAll(_codeId, '')
      .replaceAll(_bareLabelledId, '');
}
