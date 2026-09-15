#!/usr/bin/env bash
# CloudMonitor alarm rules of the trajectory deployment (SPEC §12), created or updated with
# PutCustomMetricRule on the custom metrics that push-metrics.sh reports (backend/trajectory/ops/cms.py,
# dimension instance=<instance>). Run from an operator machine with the aliyun CLI. Rule IDs are fixed
# (openbox-<instance>-<name>), so re-running updates the rules in place. Dry run unless --execute.
#
#   setup-alarms.sh [--instance gw2] [--group-id 0] [--region cn-shanghai]
#                   [--contact-group 云账号报警联系人] [--webhook URL] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

instance=gw2
group_id=${TRAJECTORY_CMS_GROUP_ID:-0}
region=cn-shanghai
contact_group=云账号报警联系人
webhook=
while [ $# -gt 0 ]; do
  case "$1" in
    --instance)
      need_value "$1" $#
      instance=$2
      shift 2
      ;;
    --group-id)
      need_value "$1" $#
      group_id=$2
      shift 2
      ;;
    --region)
      need_value "$1" $#
      region=$2
      shift 2
      ;;
    --contact-group)
      need_value "$1" $#
      contact_group=$2
      shift 2
      ;;
    --webhook)
      need_value "$1" $#
      webhook=$2
      shift 2
      ;;
    --execute)
      EXECUTE=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ $instance =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "invalid --instance: $instance"
[[ $group_id =~ ^[0-9]{1,20}$ ]] || die "--group-id must be a number"
[[ $region =~ ^[a-z0-9-]+$ ]] || die "invalid --region: $region"
[ -n "$contact_group" ] || die "--contact-group must not be empty"
if [ "$EXECUTE" = 1 ]; then
  command -v aliyun >/dev/null 2>&1 || die "the aliyun CLI is required"
fi

# name|metric|operator|threshold|statistics|period seconds|evaluations|level|subject
# Statistics: Average is the only value the PutCustomMetricRule reference documents. push-metrics.sh
# reports one sample per minute, so the average of a period is the reported value.
RULES='
host-disk|host_disk_used_percent|>=|80|Average|60|3|CRITICAL|host disk usage >= 80%
spool-bytes|spool_bytes|>=|1073741824|Average|60|2|CRITICAL|trajectory spool >= 1 GiB
spool-age|spool_oldest_age_seconds|>=|60|Average|60|3|WARN|oldest trajectory spool file >= 60 s
worker-down|worker_up|<|1|Average|60|3|CRITICAL|trajectory worker health check down
recording-gaps|gaps_recorded_1h|>|0|Average|60|1|WARN|trajectory recording gaps in the last hour
projection-lag|projection_lag_events|>=|5000|Average|60|5|WARN|trajectory projection lag >= 5000 events
blob-put-failures|blob_put_failures_5m|>=|10|Average|60|1|WARN|trajectory blob put failures >= 10 in 5 min
trace-db-size|trace_db_bytes|>=|21474836480|Average|300|1|WARN|trace database >= 20 GiB
oom-kill|oom_kills_1h|>|0|Average|60|1|CRITICAL|kernel OOM kill on the host
archive-lag|archive_lag_events|>=|50000|Average|60|15|WARN|trajectory archive lag >= 50000 events for 15 min
hot-partitions|hot_partitions|>|10|Average|300|1|WARN|trajectory hot event partitions > 10
backend-cpu|backend_cpu_percent|>|90|Average|60|5|CRITICAL|backend CPU > 90% of a core for 5 min
backend-memory|backend_mem_percent|>|90|Average|60|5|CRITICAL|backend memory > 90% of its limit for 5 min
business-trajectory-statements|business_trajectory_statements|>|0|Average|60|1|CRITICAL|trajectory statements in the business database
events-ingested-24h|events_ingested_24h|>|1000000|Average|300|1|INFO|trajectory events ingested in 24 h > 1000000'

count=0
while IFS='|' read -r name metric operator threshold statistics period evaluations level subject; do
  if [ -z "$name" ]; then
    continue
  fi
  set -- cms PutCustomMetricRule --region "$region" \
    --RuleId "openbox-$instance-$name" --RuleName "openbox-$instance-$name" \
    --GroupId "$group_id" --MetricName "$metric" \
    --Resources "[{\"groupId\":$group_id,\"dimension\":\"instance=$instance\"}]" \
    --ContactGroups "$contact_group" --Threshold "$threshold" --ComparisonOperator "$operator" \
    --Statistics "$statistics" --Period "$period" --EvaluationCount "$evaluations" --Level "$level" \
    --SilenceTime 3600 --EffectiveInterval 00:00-23:59 --EmailSubject "[OpenBox $instance] $subject"
  if [ -n "$webhook" ]; then
    set -- "$@" --Webhook "$webhook"
  fi
  if [ "$EXECUTE" = 1 ]; then
    log "PutCustomMetricRule openbox-$instance-$name ($metric $operator $threshold)"
    aliyun "$@" >/dev/null || die "PutCustomMetricRule openbox-$instance-$name failed"
  else
    log "dry-run: aliyun $*"
  fi
  count=$((count + 1))
done <<<"$RULES"

if [ "$EXECUTE" = 1 ]; then
  log "$count alarm rules created or updated"
else
  log "dry run: $count alarm rules; re-run with --execute to create or update them"
fi
