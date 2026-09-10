#!/usr/bin/env python3
"""Compile every vendor with fake, build-only configuration; never run the APK.

Refuses to touch existing private configs. Cleans up in finally. The resulting
APK is a build test artifact and must NOT be installed or distributed.
"""
import json
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["XIAOMI_APPID", "XIAOMI_APPKEY", "HONOR_APPID", "OPPO_APPID", "OPPO_APPKEY",
          "OPPO_APPSECRET", "VIVO_APPID", "VIVO_APPKEY", "MEIZU_APPID", "MEIZU_APPKEY"]
FILES = {
    ROOT / "android/push-vendors.properties": "\n".join(f"{name}=1234567890123" for name in FIELDS) + "\n",
    ROOT / "android/app/agconnect-services.json": json.dumps({
        "agcgw": {"url": "https://connect-drcn.hispace.hicloud.com"},
        "client": {"app_id": "101234567", "cp_id": "123456789", "product_id": "123456789",
                   "client_id": "123456789", "client_secret": "build-only-not-a-secret",
                   "api_key": "build-only-not-a-key", "package_name": "com.bossip.bipmobile"},
        "configuration_version": "3.0", "region": "CN"}),
    ROOT / "android/app/google-services.json": json.dumps({
        "project_info": {"project_number": "123456789000", "project_id": "openbox-build-only",
                         "storage_bucket": "openbox-build-only.appspot.com"},
        "client": [{"client_info": {"mobilesdk_app_id": "1:123456789000:android:0000000000000000",
                                    "android_client_info": {"package_name": "com.bossip.bipmobile"}},
                    "api_key": [{"current_key": "build-only-not-a-key"}], "oauth_client": [], "services": {}}],
        "configuration_version": "1"}),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', action='store_true', help='Also exercise release shrinking and packaging')
    variant = 'release' if parser.parse_args().release else 'debug'
    if any(p.exists() for p in FILES) or (ROOT / "android/key.properties").exists():
        raise SystemExit("Run this build check in a checkout without private vendor/signing configuration.")
    # Never pick up a developer's real credentials from CI environment overrides.
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOSSIP_PUSH_")}
    created = []
    try:
        for p, content in FILES.items():
            with p.open("x") as handle:
                handle.write(content)
            p.chmod(0o600)
            created.append(p)
        # Regenerate the variant's plugin registrant. --no-pub can retain a
        # debug integration_test entry that is unavailable in release.
        subprocess.run(["flutter", "build", "apk", '--' + variant], cwd=ROOT, env=env, check=True)
        verify_manifest(variant)
        print("All seven vendor adapters compiled and merged; no push was sent.")
    finally:
        for p in created:
            p.unlink(missing_ok=True)
        # Remove the fake-config outputs so they cannot be distributed by mistake.
        for directory in ("build/app/outputs/flutter-apk", 'build/app/outputs/apk/' + variant):
            shutil.rmtree(ROOT / directory, ignore_errors=True)


def verify_manifest(variant):
    import xml.etree.ElementTree as ET

    manifest = ROOT / f"build/app/intermediates/merged_manifests/{variant}/process{variant.title()}Manifest/AndroidManifest.xml"
    root = ET.parse(manifest).getroot()
    namespace = "{http://schemas.android.com/apk/res/android}"
    names = {item.get(namespace + "name") for item in root.iter()}
    # Validate SDK component inclusion, not merely a successful empty build.
    for prefix in ("com.huawei.", "com.hihonor.", "com.xiaomi.", "com.heytap.",
                   "com.vivo.", "com.meizu.", "com.google.firebase."):
        assert any(name and name.startswith(prefix) for name in names), prefix
    metadata = {item.get(namespace + "name"): item.get(namespace + "value")
                for item in root.findall(".//meta-data")}
    assert metadata["XIAOMI_APPID"] == "1234567890123"
    assert metadata["XIAOMI_APPKEY"] == "1234567890123"
    assert metadata["OPPO_APPKEY"] == "OP-1234567890123"
    assert metadata["MEIZU_APPKEY"] == "MZ-1234567890123"
    assert metadata["push_kit_auto_init_enabled"] == "false"
    assert metadata["firebase_messaging_auto_init_enabled"] == "false"
    assert metadata["com.vivo.push.app_id"] == "1234567890123"
    assert "com.bossip.bipmobile.AuthCallbackActivity" in names


if __name__ == "__main__":
    main()
