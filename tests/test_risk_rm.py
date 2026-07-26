from __future__ import annotations

import pytest

from jean.approval.risk import Risk, classify_risk

# The command that provoked this, from agent-damian in production. The pptx skill's
# own documented workflow prescribes the `rm` ("The `rm` clears stale images from
# prior runs"), so jean was gating a command its own skill told the agent to run --
# and "Always allow" could not cover it, because the compound command varied by one
# argument (`tail -20` vs `tail -5`) between the two attempts.
PPTX_RENDER = (
    "cd /tmp/pptx_build && python3 /var/lib/agent-damian/marketplaces/overlays/"
    "57c461978ba5fec4/document-skills/skills/pptx/scripts/office/soffice.py --headless "
    "--convert-to pdf /tmp/argocd-deck.pptx --outdir /tmp/pptx_build 2>&1 "
    "| tail -5 && rm -f slide-*.jpg && pdftoppm -jpeg -r 150 "
    '/tmp/pptx_build/argocd-deck.pdf slide && ls -1 "$PWD"/slide-*.jpg'
)


def _risk(command: str) -> Risk:
    return classify_risk("Bash", {"command": command})


def test_the_pptx_render_workflow_no_longer_prompts():
    assert _risk(PPTX_RENDER) is Risk.SAFE


# --- non-recursive rm of ordinary targets: the force flag alone is not danger ---


@pytest.mark.parametrize(
    "command",
    [
        "rm -f slide-*.jpg",
        "rm -f *.tmp",
        "rm -f output.pdf",
        "rm --force page-1.jpg",
        "rm -f ./build/artifact.zip",
        "rm -f /tmp/scratch.txt",
        "rm -f /tmp/pptx_build/slide-1.jpg",
    ],
)
def test_forcing_a_named_or_globbed_file_is_not_destructive(command: str):
    assert _risk(command) is Risk.SAFE


# --- recursion is danger regardless of target ---------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf build/",
        "rm -rf ./node_modules",
        "rm -fr dist",
        "rm -r -f cache",
        "rm -f -r cache",
        "rm --recursive --force out",
        "rm -r tmpdir",  # recursive without force still removes a whole tree
    ],
)
def test_recursive_removal_is_always_destructive(command: str):
    assert _risk(command) is Risk.RISKY


# --- the target is what makes a non-recursive rm dangerous --------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm -f /etc/resolv.conf",
        "rm -f /usr/bin/python3",
        "rm -f /boot/vmlinuz",
        "rm -f /var/lib/agent-damian/sessions.db",
        "rm -f ~/.bashrc",
        "rm -f /root/.ssh/authorized_keys",
        "rm -f /home/jean/soul.md",
        "rm -f /*",
    ],
)
def test_removing_a_system_or_home_path_is_destructive(command: str):
    assert _risk(command) is Risk.RISKY


def test_tmp_is_not_a_system_path():
    """The skills do their scratch work in /tmp; treating it as sensitive would
    re-break the workflow this change exists to unblock."""
    assert _risk("rm -f /tmp/anything.jpg") is Risk.SAFE
    assert _risk("rm -f /tmp/pptx_build/slide-3.jpg") is Risk.SAFE


def test_recursive_beats_the_tmp_exemption():
    """A scratch directory is a fine place to work and still a bad thing to wipe
    recursively without a human seeing it."""
    assert _risk("rm -rf /tmp/pptx_build") is Risk.RISKY


# --- unchanged behaviour ------------------------------------------------------


def test_a_plain_rm_stays_safe():
    assert _risk("rm out.txt") is Risk.SAFE


@pytest.mark.parametrize(
    "command",
    [
        "git reset --hard",
        "git clean -fd",
        "kubectl delete pod x",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sdb1",
        "sudo rm -f anything",
        "drop table users",
    ],
)
def test_every_other_destructive_rule_is_untouched(command: str):
    assert _risk(command) is Risk.RISKY


def test_secrets_still_win_even_for_a_harmless_looking_rm():
    """`rm -f .env` is not about recursion or a system path -- _SECRETS catches it,
    and must keep doing so."""
    assert _risk("rm -f .env") is Risk.RISKY
    assert _risk("rm -f id_rsa") is Risk.RISKY


# --- default-deny for absolute targets ----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm --force /data",  # regressed a first attempt that enumerated system dirs
        "rm -f /mnt/backup.tar",
        "rm -f /workspace/out.zip",
        "rm -f /srv/app.conf",
    ],
)
def test_any_absolute_target_asks_even_if_it_is_not_a_known_system_dir(command: str):
    """Enumerating dangerous directories leaked: `rm --force /data` matched nothing.
    Absolute targets are default-deny, with only the scratch paths exempt."""
    assert _risk(command) is Risk.RISKY


def test_the_agents_own_workspace_is_scratch():
    """cwd is settings.home/workspaces -- the per-thread dir where the agent writes
    its own artifacts. Gating cleanup there would recreate the same annoyance."""
    assert _risk("rm -f /var/lib/agent-anya/workspaces/abc/postmortem.pdf") is Risk.SAFE
    assert _risk("rm -f /var/lib/agent-damian/workspaces/x/slide-1.jpg") is Risk.SAFE


def test_workspaces_exemption_does_not_survive_recursion():
    assert _risk("rm -rf /var/lib/agent-anya/workspaces/abc") is Risk.RISKY


def test_var_lib_outside_workspaces_still_asks():
    """jean's transcripts and marketplace clones live under /var/lib/<agent>/ --
    outside workspaces/, removing them destroys real state."""
    assert _risk("rm -f /var/lib/agent-anya/.claude/projects/x.jsonl") is Risk.RISKY
