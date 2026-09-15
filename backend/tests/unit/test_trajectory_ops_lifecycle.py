"""OSS lifecycle merge (trajectory.ops.lifecycle): foreign rules survive, conflicts OSS rejects are refused."""
import subprocess
import sys
from pathlib import Path

import pytest

from trajectory.ops import lifecycle

REPO = Path(__file__).resolve().parents[3]
DESIRED = REPO / "deploy" / "gw2" / "oss-lifecycle.xml"
MODULE = REPO / "backend" / "trajectory" / "ops" / "lifecycle.py"


def document(*rules: str) -> str:
    return "<?xml version='1.0' encoding='UTF-8'?><LifecycleConfiguration>" + "".join(rules) + "</LifecycleConfiguration>"


def rule(identifier: str, prefix: str, body: str, status: str = "Enabled") -> str:
    return f"<Rule><ID>{identifier}</ID><Prefix>{prefix}</Prefix><Status>{status}</Status>{body}</Rule>"


FOREIGN_IA = rule(
    "assets-ia-60d",
    "assets/",
    "<Transition><Days>60</Days><StorageClass>IA</StorageClass><IsAccessTime>false</IsAccessTime></Transition>"
    "<AtimeBase>1757836800</AtimeBase>",
)


def desired() -> str:
    return DESIRED.read_text(encoding="utf-8")


def test_desired_rules_implement_the_spec():
    parsed = lifecycle.parse(desired())
    rules = {lifecycle.rule_id(item): lifecycle.semantic(item) for item in parsed}
    ia = rules["openbox-trajectories-ia-30d"]
    assert (ia["Prefix"], ia["Transition"], ia["Not"]) == (
        "trajectories/", [(("Days", "30"), ("StorageClass", "IA"))], [("trajectories/_exports/", ())]
    )
    exports = rules["openbox-trajectory-exports-expire-30d"]
    assert (exports["Prefix"], exports["Expiration"]) == ("trajectories/_exports/", [(("Days", "30"),)])
    backups = rules["openbox-postgres-backups-expire-30d"]
    assert (backups["Prefix"], backups["Expiration"], backups["AbortMultipartUpload"]) == (
        "backups/postgres/", [(("Days", "30"),)], [(("Days", "7"),)]
    )
    assert rules["openbox-trajectories-abort-multipart-7d"]["AbortMultipartUpload"] == [(("Days", "7"),)]
    assert {item["Status"] for item in rules.values()} == {"Enabled"}
    assert lifecycle.problems(parsed, set(rules), allow_same_action_overlap=False) == []
    for key in ("trajectories/trj_1/blobs/ab", "trajectories/_exports/exp_1/cd.zip", "backups/postgres/20260915/x.dump"):
        assert any(
            key.startswith(lifecycle.rule_prefix(item)) and "AbortMultipartUpload" in lifecycle.actions(item)
            for item in parsed
        )


def test_merge_into_a_bucket_without_rules():
    result = lifecycle.merge("", desired())
    assert result.changed and result.kept == [] and result.updated == []
    assert result.added == [lifecycle.rule_id(item) for item in lifecycle.parse(desired())]
    assert lifecycle.verify(desired(), result.xml()) == []


def test_merge_keeps_foreign_rules_in_place_without_response_only_elements():
    existing = document(FOREIGN_IA, rule("tmp-expire", "_deploy-tmp/", "<Expiration><Days>1</Days></Expiration>"))
    result = lifecycle.merge(existing, desired())
    merged = lifecycle.parse(result.xml())
    assert [lifecycle.rule_id(item) for item in merged][:2] == ["assets-ia-60d", "tmp-expire"]
    assert len(merged) == 6 and result.kept == ["assets-ia-60d", "tmp-expire"]
    assert "AtimeBase" not in result.xml()
    assert lifecycle.semantic(merged[0]) == lifecycle.semantic(lifecycle.parse(existing)[0])


def test_merge_updates_a_managed_rule_where_it_was():
    stale = rule("openbox-postgres-backups-expire-30d", "backups/postgres/", "<Expiration><Days>7</Days></Expiration>")
    result = lifecycle.merge(document(stale, FOREIGN_IA), desired())
    assert result.updated == ["openbox-postgres-backups-expire-30d"] and result.kept == ["assets-ia-60d"]
    merged = lifecycle.parse(result.xml())
    assert lifecycle.rule_id(merged[0]) == "openbox-postgres-backups-expire-30d"
    assert lifecycle.semantic(merged[0])["Expiration"] == [(("Days", "30"),)]


def test_rules_echoed_with_default_flags_are_unchanged():
    echoed = desired().replace(
        "<StorageClass>IA</StorageClass>",
        "<StorageClass>IA</StorageClass><IsAccessTime>false</IsAccessTime><ReturnToStdWhenVisit>false</ReturnToStdWhenVisit>",
    )
    result = lifecycle.merge(echoed, desired())
    assert not result.changed and len(result.unchanged) == 4


def test_same_action_on_overlapping_prefixes_needs_explicit_permission():
    bucket_expiry = rule("expire-everything-365d", "", "<Expiration><Days>365</Days></Expiration>")
    with pytest.raises(lifecycle.LifecycleError) as raised:
        lifecycle.merge(document(bucket_expiry), desired())
    assert any("expire-everything-365d" in problem and "Expiration" in problem for problem in raised.value.problems)
    assert lifecycle.merge(document(bucket_expiry), desired(), allow_same_action_overlap=True).changed


def test_overlapping_part_policies_are_always_refused():
    bucket_parts = rule("abort-parts-3d", "", "<AbortMultipartUpload><Days>3</Days></AbortMultipartUpload>")
    with pytest.raises(lifecycle.LifecycleError, match="abort multipart"):
        lifecycle.merge(document(bucket_parts), desired(), allow_same_action_overlap=True)


def test_foreign_rules_on_other_prefixes_do_not_conflict():
    other = rule(
        "assets-cleanup", "assets/",
        "<Expiration><Days>90</Days></Expiration><AbortMultipartUpload><Days>1</Days></AbortMultipartUpload>",
    )
    assert lifecycle.merge(document(other), desired()).kept == ["assets-cleanup"]


def test_not_filters_decide_overlap():
    ia, exports, abort, backups = lifecycle.parse(desired())
    assert not lifecycle.overlaps(ia, exports)
    assert lifecycle.overlaps(ia, abort) and lifecycle.overlaps(abort, exports)
    assert not lifecycle.overlaps(ia, backups)
    nested = lifecycle.parse(document(rule(
        "old-exports", "trajectories/_exports/old/",
        "<Transition><Days>90</Days><StorageClass>Archive</StorageClass></Transition>",
    )))[0]
    assert not lifecycle.overlaps(ia, nested)
    tagged_not = lifecycle.parse(document(rule(
        "tagged", "trajectories/",
        "<Filter><Not><Prefix>trajectories/_exports/</Prefix><Tag><Key>k</Key><Value>v</Value></Tag></Not></Filter>"
        "<Transition><Days>40</Days><StorageClass>IA</StorageClass></Transition>",
    )))[0]
    assert lifecycle.overlaps(tagged_not, exports)


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (
            "<Filter><Not><Prefix>trajectories/_exports/</Prefix></Not></Filter>"
            "<AbortMultipartUpload><Days>7</Days></AbortMultipartUpload>",
            "cannot be combined",
        ),
        ("<Filter><Not><Prefix>other/</Prefix></Not></Filter><Expiration><Days>7</Days></Expiration>", "must start with"),
        ("<Filter><Not><Prefix>trajectories/</Prefix></Not></Filter><Expiration><Days>7</Days></Expiration>", "must differ"),
    ],
)
def test_invalid_managed_rules_are_refused(body, problem):
    with pytest.raises(lifecycle.LifecycleError, match=problem):
        lifecycle.merge("", document(rule("openbox-bad", "trajectories/", body)))


def test_desired_rules_need_unique_ids():
    expire = "<Expiration><Days>1</Days></Expiration>"
    with pytest.raises(lifecycle.LifecycleError, match="unique ID"):
        lifecycle.merge("", document(rule("", "a/", expire)))
    with pytest.raises(lifecycle.LifecycleError, match="unique ID"):
        lifecycle.merge("", document(rule("same", "a/", expire), rule("same", "b/", expire)))
    with pytest.raises(lifecycle.LifecycleError, match="no rules"):
        lifecycle.merge("", "<LifecycleConfiguration/>")


def test_verify_reports_missing_changed_and_unexpected_rules():
    applied = lifecycle.merge(document(FOREIGN_IA), desired()).xml()
    assert lifecycle.verify(applied, applied) == []
    drifted = applied.replace("<Days>30</Days>", "<Days>31</Days>", 1)
    assert any(problem.startswith("rule openbox-trajectories-ia-30d differs") for problem in lifecycle.verify(applied, drifted))
    only_managed = lifecycle.merge("", desired()).xml()
    assert lifecycle.verify(applied, only_managed) == ["rule assets-ia-60d is missing"]
    assert lifecycle.verify(only_managed, applied) == ["unexpected rule assets-ia-60d"]


def test_parse_tolerates_output_around_the_document():
    noisy = "Warning: using profile default\n" + document(FOREIGN_IA) + "\n0.123456(s) elapsed\n"
    assert [lifecycle.rule_id(item) for item in lifecycle.parse(noisy)] == ["assets-ia-60d"]
    assert lifecycle.parse("<LifecycleConfiguration/>") == []
    namespaced = f'<LifecycleConfiguration xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com">{FOREIGN_IA}</LifecycleConfiguration>'
    assert lifecycle.rule_id(lifecycle.parse(namespaced)[0]) == "assets-ia-60d"
    for broken in ("Error: NoSuchLifecycle", "<LifecycleConfiguration><Rule>"):
        with pytest.raises(lifecycle.LifecycleError):
            lifecycle.parse(broken)


def test_cli_exit_codes(tmp_path):
    existing = tmp_path / "existing.xml"
    existing.write_text("")
    merged = tmp_path / "merged.xml"
    base = ["--desired", str(DESIRED)]
    assert lifecycle.main(["merge", "--existing", str(existing), *base, "--output", str(merged)]) == 0
    again = tmp_path / "again.xml"
    assert lifecycle.main(["merge", "--existing", str(merged), *base, "--output", str(again)]) == lifecycle.UNCHANGED
    assert lifecycle.main(["verify", "--expected", str(merged), "--actual", str(again)]) == 0
    assert lifecycle.main(["verify", "--expected", str(merged), "--actual", str(existing)]) == 1
    conflict = tmp_path / "conflict.xml"
    conflict.write_text(document(rule("abort-all", "", "<AbortMultipartUpload><Days>1</Days></AbortMultipartUpload>")))
    assert lifecycle.main(["merge", "--existing", str(conflict), *base, "--output", str(tmp_path / "x.xml")]) == 2
    assert lifecycle.main(["show", str(tmp_path / "missing.xml")]) == 2


def test_module_runs_as_a_plain_python_script(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-I", str(MODULE), "show", str(DESIRED)],
        capture_output=True, text=True, cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("\n") == 4 and "openbox-trajectories-ia-30d" in completed.stdout
