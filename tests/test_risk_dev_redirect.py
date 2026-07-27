from __future__ import annotations

import pytest

from jean.approval.risk import Risk, classify_risk


def _risk(command: str) -> Risk:
    return classify_risk("Bash", {"command": command})


# --- the false positive this exists for ------------------------------------
#
# `>\s*/dev/` was written to catch a write to a block device. It also matched
# `2>/dev/null`, so every command that silences stderr was classified
# DESTRUCTIVE and interrupted a human. Measured against real traffic: 2 of 155
# tool calls prompted, and both were `ls ... 2>/dev/null`.


@pytest.mark.parametrize(
    "command",
    [
        "ls ~/Work/ 2>/dev/null",
        "ls ~/Work/ 2>/dev/null || ls /home/ 2>/dev/null",
        "kubectl get pods 2>/dev/null",
        "grep -r foo . 2>/dev/null",
        "curl -s example.com >/dev/null 2>&1",
        "make build > /dev/null",
        "echo hi > /dev/stdout",
        "echo oops > /dev/stderr",
        "cat banner > /dev/tty",
        "cmd >/dev/fd/1",
    ],
)
def test_writing_to_a_harmless_pseudo_device_is_not_destructive(command: str):
    from jean.approval.risk import _DESTRUCTIVE

    assert not _DESTRUCTIVE.search(command), f"{command!r} matched _DESTRUCTIVE"


def test_silencing_stderr_does_not_make_a_read_only_command_risky():
    assert _risk("ls ~/Work/ 2>/dev/null") is Risk.SAFE
    assert _risk("kubectl get pods -n devops 2>/dev/null") is Risk.SAFE


def test_a_redirect_does_not_change_what_a_fetch_is_judged_on():
    """The redirect is harmless either way; the METHOD decides. This guards against
    a fix to the /dev/ pattern that loosens or tightens more than the redirect."""
    assert _risk("curl -s https://example.com >/dev/null 2>&1") is Risk.SAFE
    assert _risk("curl -X POST https://example.com >/dev/null 2>&1") is Risk.RISKY


# --- what the pattern was actually for -------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat image.iso > /dev/sda",
        "cat image.iso >/dev/sdb1",
        "tar cf - . > /dev/nvme0n1",
        "cmd > /dev/vda",
        "cmd > /dev/hda",
        "cmd > /dev/mmcblk0",
        "cmd > /dev/disk2",
        "cmd > /dev/mapper/vg-root",
        "cmd 2> /dev/sdb",  # a device write is a device write on any fd
    ],
)
def test_writing_to_a_real_device_is_still_destructive(command: str):
    assert _risk(command) is Risk.RISKY


def test_an_unrecognised_dev_target_still_asks():
    """Default-deny for anything under /dev/ that is not a known-harmless
    pseudo-device: an unfamiliar device node is exactly when a human should look."""
    assert _risk("cmd > /dev/some-new-thing") is Risk.RISKY


def test_dd_to_a_device_is_unaffected():
    assert _risk("dd if=/dev/zero of=/dev/sda bs=1M") is Risk.RISKY


def test_reading_from_a_device_is_not_a_write():
    """`< /dev/urandom` and `if=/dev/zero` read; only the redirect direction that
    writes should trip this rule (dd is caught separately by its own pattern)."""
    from jean.approval.risk import _DESTRUCTIVE

    assert not _DESTRUCTIVE.search("head -c 32 < /dev/urandom | base64")
