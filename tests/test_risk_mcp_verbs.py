from __future__ import annotations

import pytest

from jean.approval.risk import Risk, classify_risk


def _risk(tool: str) -> Risk:
    return classify_risk(tool, {})


# --- the gap this closes ---------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    [
        # Argo CD: `sync` IS a deploy, and `run_resource_action` is how a restart,
        # a pause or any other Lua resource action is invoked. Neither word was in
        # the verb list, so both ran unattended -- an agent could ship to
        # production without an approval click.
        "mcp__plugin_argocd_argocd__sync_application",
        "mcp__plugin_argocd_argocd__run_resource_action",
    ],
)
def test_argocd_deploy_verbs_ask(tool: str):
    assert _risk(tool) is Risk.RISKY


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__plugin_x_y__resync_repo",
        "mcp__plugin_x_y__run_workflow_action",
        "mcp__plugin_x_y__trigger_action",
        "mcp__plugin_x_y__rollback_release",
        "mcp__plugin_x_y__promote_release",
        "mcp__plugin_x_y__abort_rollout",
        "mcp__plugin_x_y__suspend_resource",
        "mcp__plugin_x_y__resume_resource",
    ],
)
def test_other_effecting_verbs_ask(tool: str):
    """The same class of word: it makes something happen elsewhere. Added
    together so the next server with a synonym is covered before it ships."""
    assert _risk(tool) is Risk.RISKY


# --- reads stay silent -----------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__plugin_argocd_argocd__list_applications",
        "mcp__plugin_argocd_argocd__get_application",
        "mcp__plugin_argocd_argocd__get_resource_actions",
        "mcp__plugin_argocd_argocd__get_application_resource_tree",
        "mcp__plugin_argocd_argocd__list_clusters",
        "mcp__plugin_argocd_argocd__get_appproject",
        "mcp__plugin_kubectl_kubernetes__pods_list",
        "mcp__plugin_grafana_grafana__query_prometheus",
    ],
)
def test_reads_do_not_ask(tool: str):
    """Routine work must not start costing a click -- that is the friction the
    whole classifier exists to avoid."""
    assert _risk(tool) is Risk.SAFE


def test_get_resource_actions_is_a_read_despite_the_word_action():
    """Listing which actions EXIST is how the agent discovers `restart` before
    asking to run it. Matching a bare `action` would gate the lookup too, and the
    agent would need approval merely to find out what it could ask for."""
    assert _risk("mcp__plugin_argocd_argocd__get_resource_actions") is Risk.SAFE


def test_unsuspend_is_not_suspend():
    """Same negation trap the cordon rule already documents."""
    assert _risk("mcp__plugin_x_y__unsuspend_resource") is Risk.SAFE


# --- the existing verbs are untouched --------------------------------------


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__plugin_kubectl_kubernetes__pods_delete",
        "mcp__plugin_argocd_argocd__delete_application",
        "mcp__plugin_argocd_argocd__create_application",
        "mcp__plugin_argocd_argocd__update_application",
        "mcp__plugin_kubectl_kubernetes__resources_scale",
    ],
)
def test_previously_risky_verbs_still_ask(tool: str):
    assert _risk(tool) is Risk.RISKY
