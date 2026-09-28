"""
Render per-task env files for the ECS-local stack, the way the ECS agent
builds a container's environment from its task definition:

- `environment` entries from ecs/taskdef/<task>.json, with values overridden
  by ecs.local.env (Service Connect names, local endpoints; ${NAME} expanded
  from .env);
- `secrets` entries resolved with Secrets Manager GetSecretValue against
  LocalStack, by the exact `valueFrom` ARN in the task definition (the
  execution-role step in real ECS).

Output: .ecs-local/env/<task>.env, one KEY=VALUE per line, consumed by
compose.ecs.local.yml with `format: raw` (no interpolation). Secret values are
never printed.

    python3 scripts/ecs_local_env.py

Inputs are fixed repo paths and the LocalStack port published by
compose.ecs.local.yml; there are no CLI arguments.

Stdlib only so it runs on the host without the project venv.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOTENV = ROOT / ".env"
OVERRIDES = ROOT / "ecs.local.env"
OUT_DIR = ROOT / ".ecs-local" / "env"
LOCALSTACK_ENDPOINT = "http://127.0.0.1:4566"
TASKDEF_DIR = ROOT / "ecs" / "taskdef"
REF = re.compile(r"\$\{([A-Za-z_]\w*)\}", re.ASCII)
# LocalStack derives region/account from the credential scope; the signature
# itself is not verified.
AUTH = (
    "AWS4-HMAC-SHA256 Credential=test/20260101/{region}/secretsmanager/aws4_request, "
    "SignedHeaders=host;x-amz-target, Signature=0"
)


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def expand(value: str, source: dict[str, str]) -> str:
    return REF.sub(lambda m: source.get(m.group(1), ""), value)


def get_secret(value_from: str) -> str:
    # valueFrom: arn:aws:secretsmanager:region:account:secret:name[:json-key:version-stage:version-id]
    parts = value_from.split(":")
    if len(parts) < 7 or parts[2] != "secretsmanager":
        raise ValueError(f"not a Secrets Manager ARN: {value_from}")
    region = parts[3]
    secret_id = ":".join(parts[:7])
    json_key = parts[7] if len(parts) > 7 else ""
    body: dict[str, str] = {"SecretId": secret_id}
    if len(parts) > 8 and parts[8]:
        body["VersionStage"] = parts[8]
    if len(parts) > 9 and parts[9]:
        body["VersionId"] = parts[9]
    req = urllib.request.Request(
        LOCALSTACK_ENDPOINT + "/",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "secretsmanager.GetSecretValue",
            "Authorization": AUTH.format(region=region),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            secret = json.load(resp)["SecretString"]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise RuntimeError(f"GetSecretValue {secret_id} -> HTTP {exc.code}: {detail}") from None
    return str(json.loads(secret)[json_key]) if json_key else secret


def render(taskdef: dict, overrides: dict[str, str]) -> dict[str, str]:
    (container,) = [c for c in taskdef["containerDefinitions"] if c.get("essential", True)]
    env = {
        e["name"]: overrides.get(e["name"], e["value"]) for e in container.get("environment", [])
    }
    for secret in container.get("secrets", []):
        env[secret["name"]] = get_secret(secret["valueFrom"])
    for key, value in env.items():
        if "\n" in value:
            raise ValueError(f"{key}: multi-line values are not supported in env files")
    return env


def main() -> int:
    dotenv = parse_dotenv(DOTENV)
    overrides = {k: expand(v, dotenv) for k, v in parse_dotenv(OVERRIDES).items()}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(TASKDEF_DIR.glob("*.json")):
        taskdef = json.loads(path.read_text())
        env = render(taskdef, overrides)
        target = OUT_DIR / f"{path.stem}.env"
        target.write_text("".join(f"{k}={v}\n" for k, v in sorted(env.items())))
        target.chmod(0o600)
        n_secrets = sum(len(c.get("secrets", [])) for c in taskdef["containerDefinitions"])
        print(
            f"rendered {target.relative_to(ROOT)}: {len(env) - n_secrets} environment, "
            f"{n_secrets} secrets from Secrets Manager ({taskdef['family']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
