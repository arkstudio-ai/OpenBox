#!/bin/bash
# Run a local shell script on the light-test machine through Aliyun Cloud Assistant and print its output.
#
#   deploy/light-test/scripts/ecsrun.sh <script-file> [timeout-seconds]
#
# The machine has no SSH; Cloud Assistant is the only way in. Never run two of these
# against the same instance at once. If RunCommand itself times out on the client side,
# the command may still be running remotely: this script then looks it up with
# DescribeInvocations instead of sending it again.
#
# Env: LT_INSTANCE (default openbox-light-test), LT_REGION (default cn-shanghai).
set -uo pipefail
INSTANCE=${LT_INSTANCE:-i-uf6gdzj4pv1hmdlexbf8}
REGION=${LT_REGION:-cn-shanghai}
SCRIPT=${1:?usage: ecsrun.sh <script-file> [timeout-seconds]}
TIMEOUT=${2:-300}
[ -f "$SCRIPT" ] || { echo "no such script: $SCRIPT" >&2; exit 2; }
head -1 "$SCRIPT" | grep -q '^#!' || { echo "script must start with a #! line" >&2; exit 2; }

B64=$(base64 < "$SCRIPT" | tr -d '\n')
NAME="ecsrun-$(date +%Y%m%d%H%M%S)-$$"
OUT=$(aliyun ecs RunCommand --RegionId "$REGION" --InstanceId.1 "$INSTANCE" --Type RunShellScript \
  --Name "$NAME" --CommandContent "$B64" --ContentEncoding Base64 --Timeout "$TIMEOUT" \
  --read-timeout 90 --connect-timeout 20 2>&1)
INVOKE=$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["InvokeId"])' 2>/dev/null)
if [ -z "$INVOKE" ]; then
  # Client-side timeout: the command may have been accepted anyway. Find it by name.
  sleep 5
  INVOKE=$(aliyun ecs DescribeInvocations --RegionId "$REGION" --InstanceId "$INSTANCE" --CommandName "$NAME" \
    --read-timeout 60 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin)["Invocations"]["Invocation"]; print(d[0]["InvokeId"] if d else "")' 2>/dev/null)
  [ -n "$INVOKE" ] || { echo "RunCommand failed: $(printf '%s' "$OUT" | head -c 600)" >&2; exit 1; }
  echo "[ecsrun] RunCommand timed out on the client but was accepted: $INVOKE" >&2
fi

POLLS=$(( TIMEOUT / 5 + 6 ))
for _ in $(seq 1 "$POLLS"); do
  sleep 5
  RES=$(aliyun ecs DescribeInvocationResults --RegionId "$REGION" --InvokeId "$INVOKE" --read-timeout 60 2>/dev/null) || continue
  STATUS=$(printf '%s' "$RES" | python3 -c 'import sys,json; r=json.load(sys.stdin)["Invocation"]["InvocationResults"]["InvocationResult"][0]; print(r["InvocationStatus"])' 2>/dev/null)
  case "$STATUS" in Running|Pending|Scheduled|"") continue ;; esac
  printf '%s' "$RES" | python3 -c '
import sys, json, base64
r = json.load(sys.stdin)["Invocation"]["InvocationResults"]["InvocationResult"][0]
print("[ecsrun]", r["InvocationStatus"], "exit", r.get("ExitCode"), file=sys.stderr)
sys.stdout.write(base64.b64decode(r.get("Output") or "").decode("utf-8", "replace"))
sys.exit(0 if r["InvocationStatus"] == "Success" else 1)'
  exit $?
done
echo "[ecsrun] still running after ${TIMEOUT}s; InvokeId=$INVOKE (check DescribeInvocationResults before re-running)" >&2
exit 3
