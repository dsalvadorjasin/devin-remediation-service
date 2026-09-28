"""
Offline shape check of ecs/taskdef/*.json and ecs/service/*.json against the
botocore ECS service model (RegisterTaskDefinition / CreateService input
shapes): catches unknown keys, wrong types and missing required members
without an AWS account. This is NOT control-plane validation (Fargate cpu/
memory combos, IAM, networking) - that is Phase 2.

Needs botocore; the verify script runs it inside the LocalStack container:

    docker compose ... exec -T localstack python3 - < scripts/ecs_validate_defs.py
"""

import json
import os
import sys
from pathlib import Path

import botocore.session
from botocore.validate import ParamValidator

DEFS = Path(os.getenv("ECS_DEFS_DIR", "/ecs-defs"))
CHECKS = (("taskdef", "RegisterTaskDefinition"), ("service", "CreateService"))


def main() -> int:
    model = botocore.session.get_session().get_service_model("ecs")
    validator = ParamValidator()
    failures = 0
    for subdir, operation in CHECKS:
        shape = model.operation_model(operation).input_shape
        files = sorted((DEFS / subdir).glob("*.json"))
        if not files:
            print(f"FAIL no {subdir} definitions under {DEFS / subdir}")
            failures += 1
        for path in files:
            report = validator.validate(json.loads(path.read_text()), shape)
            if report.has_errors():
                failures += 1
                print(f"FAIL {subdir}/{path.name} ({operation}):\n{report.generate_report()}")
            else:
                print(f"ok   {subdir}/{path.name} ({operation})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
