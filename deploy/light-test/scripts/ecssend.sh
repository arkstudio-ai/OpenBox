#!/bin/bash
# Copy one local file (≤ 24 KB) to the light-test machine through Cloud Assistant SendFile.
#
#   deploy/light-test/scripts/ecssend.sh <local-file> <remote-dir> [remote-name]
#
# For scripts too big for RunCommand. Larger payloads (images, archives) go through
# oss://bossip/_deploy-tmp/ and a signed URL instead (README「发布」).
set -uo pipefail
INSTANCE=${LT_INSTANCE:-i-uf6gdzj4pv1hmdlexbf8}
REGION=${LT_REGION:-cn-shanghai}
FILE=${1:?usage: ecssend.sh <local-file> <remote-dir> [remote-name]}
DIR=${2:?remote dir}
NAME=${3:-$(basename "$FILE")}
B64=$(base64 < "$FILE" | tr -d '\n')
[ ${#B64} -le 32000 ] || { echo "file is ${#B64} bytes after base64 (SendFile limit 32 KB)" >&2; exit 2; }
OUT=$(aliyun ecs SendFile --RegionId "$REGION" --InstanceId.1 "$INSTANCE" --Name "$NAME" --TargetDir "$DIR" \
  --Content "$B64" --ContentType Base64 --Overwrite true --read-timeout 90 --connect-timeout 20 2>&1)
INVOKE=$(printf '%s' "$OUT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["InvokeId"])' 2>/dev/null)
[ -n "$INVOKE" ] || { echo "SendFile failed: $(printf '%s' "$OUT" | head -c 600)" >&2; exit 1; }
for _ in $(seq 1 24); do
  sleep 5
  STATUS=$(aliyun ecs DescribeSendFileResults --RegionId "$REGION" --InvokeId "$INVOKE" --read-timeout 60 2>/dev/null \
    | python3 -c 'import sys,json; r=json.load(sys.stdin)["Invocations"]["Invocation"][0]; print(r["InvocationStatus"])' 2>/dev/null)
  case "$STATUS" in Success) echo "[ecssend] $NAME -> $DIR"; exit 0 ;; Failed|Invalid|Aborted) echo "[ecssend] $STATUS" >&2; exit 1 ;; esac
done
echo "[ecssend] no result yet; InvokeId=$INVOKE" >&2; exit 3
