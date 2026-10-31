"""Credenciais AWS dos workers.

Uma única identidade "hub" assume as roles CloudViewReadOnly / CloudViewOperator nas contas:
- `default`: cadeia padrão do boto3 (instance profile da EC2, variáveis de ambiente, ~/.aws);
- `vault`: chave de acesso de um usuário IAM restrito a sts:AssumeRole, guardada cifrada no cofre.

As credenciais temporárias de cada role ficam só em memória, até 5 minutos antes de expirar.
"""

import json
import re
import threading
import time

import boto3
from botocore.config import Config
from sqlalchemy.orm import Session

from app.models import PlatformSetting
from app.services.secrets import read_secret
from app.vault import Vault

HUB_SECRET_KEY = "aws_hub_secret_id"  # noqa: S105 - nome da configuração, não um segredo
ROLE_ARN_RE = re.compile(r"^arn:aws(?:-[a-z]+)*:iam::(\d{12}):role/[\w+=,.@/-]{1,128}$")
BOTO_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 8}, connect_timeout=10, read_timeout=60)


class AwsAccessError(Exception):
    pass


def account_from_role_arn(arn: str) -> str:
    m = ROLE_ARN_RE.match(arn or "")
    if not m:
        raise ValueError("ARN de role inválido (arn:aws:iam::<conta>:role/<nome>)")
    return m.group(1)


def role_arn_for(account_id: str, role_name: str, partition: str = "aws") -> str:
    return f"arn:{partition}:iam::{account_id}:role/{role_name}"


class AwsHub:
    def __init__(self, base_session: boto3.Session):
        self.base = base_session
        self._cache: dict[str, tuple[float, boto3.Session]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, db: Session, vault: Vault) -> "AwsHub":
        setting = db.get(PlatformSetting, HUB_SECRET_KEY)
        if setting is None:
            return cls(boto3.Session())
        creds = json.loads(read_secret(db, vault, setting.value))
        return cls(
            boto3.Session(
                aws_access_key_id=creds["access_key_id"], aws_secret_access_key=creds["secret_access_key"]
            )
        )

    def assume(
        self, role_arn: str, external_id: str | None, session_name: str = "cloudview"
    ) -> boto3.Session:
        key = f"{role_arn}|{external_id}"
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > time.time() + 300:
                return hit[1]
        sts = self.base.client("sts", config=BOTO_CONFIG)
        params = {"RoleArn": role_arn, "RoleSessionName": session_name[:64], "DurationSeconds": 3600}
        if external_id:
            params["ExternalId"] = external_id
        try:
            creds = sts.assume_role(**params)["Credentials"]
        except Exception as e:  # noqa: BLE001
            raise AwsAccessError(f"não foi possível assumir {role_arn}: {_short(e)}") from e
        sess = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        with self._lock:
            self._cache[key] = (creds["Expiration"].timestamp(), sess)
        return sess


def _short(e: Exception) -> str:
    code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
    return code or e.__class__.__name__
