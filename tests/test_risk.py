from __future__ import annotations

import pytest

from jean.approval.risk import Risk, classify_risk


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/x",
        "git push --force origin main",
        "kubectl delete pod api-0",
        "psql -c 'DROP TABLE users'",
        "psql -c 'DELETE FROM users'",
        "git reset --hard HEAD~3",
    ],
)
def test_destructive_bash_is_risky(command):
    assert classify_risk("Bash", {"command": command}) is Risk.RISKY


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "kubectl get secret db-creds -o yaml",
        "cat ~/.ssh/id_rsa",
        "vault kv get secret/prod",
    ],
)
def test_secret_bash_is_risky(command):
    assert classify_risk("Bash", {"command": command}) is Risk.RISKY


@pytest.mark.parametrize(
    "command",
    ["curl https://api.example.com", "wget http://x/y", "gh pr create", "npm publish"],
)
def test_external_bash_is_risky(command):
    assert classify_risk("Bash", {"command": command}) is Risk.RISKY


@pytest.mark.parametrize(
    "command",
    [
        "kubectl apply -f deploy.yaml",
        "kubectl rollout restart deploy/api",
        "terraform apply",
        "helm upgrade api ./chart",
        "pip install requests",
        "npm install",
    ],
)
def test_prod_infra_bash_is_risky(command):
    assert classify_risk("Bash", {"command": command}) is Risk.RISKY


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "ls -la",
        "kubectl get pods",
        "git commit -m 'wip'",
        "git status",
        "cat src/jean/config.py",
    ],
)
def test_routine_bash_is_safe(command):
    assert classify_risk("Bash", {"command": command}) is Risk.SAFE


def test_classifier_reads_the_command_not_the_description():
    # The model's paraphrase must never soften a real command.
    verdict = classify_risk(
        "Bash", {"command": "rm -rf /data", "description": "clean up a temp file"}
    )
    assert verdict is Risk.RISKY


def test_workspace_file_write_is_safe():
    assert classify_risk("Write", {"file_path": "/home/jean/workspaces/app/main.py"}) is Risk.SAFE


@pytest.mark.parametrize("path", ["/app/.env", "/home/u/.ssh/id_rsa", "/etc/secrets/db.pem"])
def test_writing_a_secret_file_is_risky(path):
    assert classify_risk("Write", {"file_path": path}) is Risk.RISKY
    assert classify_risk("Edit", {"file_path": path}) is Risk.RISKY


def test_mcp_delete_tool_is_risky():
    assert classify_risk("mcp__plugin_kubectl_kubernetes__pods_delete", {}) is Risk.RISKY


def test_mcp_apply_tool_is_risky():
    assert classify_risk("mcp__plugin_kubectl_kubernetes__apply", {}) is Risk.RISKY


def test_synthesized_oauth_tool_is_denied():
    assert classify_risk("mcp__plugin_foo__authenticate", {}) is Risk.DENY
    assert classify_risk("mcp__plugin_foo__complete_authentication", {}) is Risk.DENY


def test_unknown_tool_defaults_to_safe():
    # The four categories are the agreed line; anything unmatched must not block.
    assert classify_risk("SomeNewTool", {"whatever": 1}) is Risk.SAFE
