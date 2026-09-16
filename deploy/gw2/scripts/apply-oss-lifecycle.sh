#!/usr/bin/env bash
# Merge deploy/gw2/oss-lifecycle.xml into a bucket's lifecycle rules (SPEC §12). Run from an operator
# machine in a repository checkout, with the aliyun CLI (ossutil 2.0 as `aliyun ossutil`) and python3.
# PutBucketLifecycle replaces every rule of the bucket, so the current rules are read first (a bucket
# without rules answers NoSuchLifecycle) and merged by rule ID with backend/trajectory/ops/lifecycle.py,
# which keeps all other rules and refuses combinations OSS rejects. With --execute the previous rules
# are saved to --backup-dir, the merged rules are applied and read back for verification.
# Dry run unless --execute.
#
#   apply-oss-lifecycle.sh --bucket NAME [--region cn-shanghai] [--profile NAME]
#                          [--allow-same-action-overlap] [--backup-dir DIR] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

bucket=
region=cn-shanghai
allow_overlap=0
backup_dir=.
while [ $# -gt 0 ]; do
  case "$1" in
    --bucket)
      need_value "$1" $#
      bucket=$2
      shift 2
      ;;
    --region)
      need_value "$1" $#
      region=$2
      shift 2
      ;;
    --profile)
      need_value "$1" $#
      export ALIBABA_CLOUD_PROFILE=$2
      shift 2
      ;;
    --allow-same-action-overlap)
      allow_overlap=1
      shift
      ;;
    --backup-dir)
      need_value "$1" $#
      backup_dir=$2
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
[[ $bucket =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || die "--bucket must be an OSS bucket name"
[[ $region =~ ^[a-z0-9-]+$ ]] || die "invalid --region: $region"
command -v aliyun >/dev/null 2>&1 || die "the aliyun CLI is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
merge_tool=$DEPLOY_DIR/../../backend/trajectory/ops/lifecycle.py
[ -f "$merge_tool" ] || die "run the script from a repository checkout ($merge_tool is missing)"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

get_rules() {  # get_rules FILE: 0 with the rules in FILE, 3 when the bucket has none, 1 on errors
  if aliyun ossutil api get-bucket-lifecycle --bucket "$bucket" --region "$region" >"$1" 2>"$work/get.err"; then
    return 0
  fi
  if grep -q NoSuchLifecycle "$1" "$work/get.err"; then
    : >"$1"
    return 3
  fi
  cat "$work/get.err" >&2
  return 1
}

log "reading the lifecycle rules of oss://$bucket"
status=0
get_rules "$work/existing.xml" || status=$?
case "$status" in
  0) ;;
  3) log "the bucket has no lifecycle rules" ;;
  *) die "could not read the lifecycle rules; nothing was changed" ;;
esac

set -- merge --existing "$work/existing.xml" --desired "$DEPLOY_DIR/oss-lifecycle.xml" --output "$work/merged.xml"
if [ "$allow_overlap" = 1 ]; then
  set -- "$@" --allow-same-action-overlap
fi
status=0
python3 "$merge_tool" "$@" || status=$?
case "$status" in
  0) ;;
  3)
    log "the bucket already has these rules; nothing to apply"
    exit 0
    ;;
  *) die "the merge was refused (see above); nothing was changed" ;;
esac

if [ "$EXECUTE" != 1 ]; then
  log "dry run: the merged configuration follows; re-run with --execute to apply it"
  cat "$work/merged.xml"
  exit 0
fi

mkdir -p "$backup_dir"
backup="$backup_dir/oss-lifecycle-$bucket-$(date -u +%Y%m%dT%H%M%SZ).xml"
cp "$work/existing.xml" "$backup"
log "saved the previous rules to $backup (an empty file means the bucket had none)"

set -- api put-bucket-lifecycle --bucket "$bucket" --lifecycle-configuration "file://$work/merged.xml" --region "$region"
if [ "$allow_overlap" = 1 ]; then
  set -- "$@" --allow-same-action-overlap true
fi
aliyun ossutil "$@" || die "put-bucket-lifecycle failed; the previous rules are in $backup"

get_rules "$work/applied.xml" || die "could not read the rules back; check the bucket and $backup"
python3 "$merge_tool" verify --expected "$work/merged.xml" --actual "$work/applied.xml" ||
  die "the bucket's rules differ from the merged configuration; restore $backup if needed (RUNBOOK.md)"
log "lifecycle rules applied and verified"
