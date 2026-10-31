"""Cofre de credenciais das nuvens, independente de provedor.

Criptografia híbrida (envelope):
  - cada segredo é cifrado com uma chave AES-256-GCM aleatória (DEK);
  - a DEK é cifrada com a chave pública RSA-4096 (OAEP-SHA256) do cofre.

A API recebe só a chave PÚBLICA: consegue gravar credenciais novas, mas não ler as existentes.
Só os workers (coleta e ações) montam a chave PRIVADA. O texto cifrado fica no Postgres
(`cloud_secret`), amarrado ao seu identificador (AAD), então não pode ser copiado para outra conexão.

Gerar o par de chaves:  python -m app.vault keygen <diretório>
"""

import base64
import json
import os
import sys
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
FORMAT = 1


class VaultError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def key_id(public_pem: bytes) -> str:
    pub = serialization.load_pem_public_key(public_pem)
    der = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    return digest.finalize().hex()[:16]


class Vault:
    def __init__(self, public_pem: bytes | None, private_pems: list[bytes] | None = None):
        self._pub = serialization.load_pem_public_key(public_pem) if public_pem else None
        self._kid = key_id(public_pem) if public_pem else None
        # Várias chaves privadas permitem rotação: a nova cifra, as antigas ainda abrem
        self._privs = {}
        for pem in private_pems or []:
            priv = serialization.load_pem_private_key(pem, password=None)
            pub_pem = priv.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            self._privs[key_id(pub_pem)] = priv

    @property
    def can_open(self) -> bool:
        return bool(self._privs)

    def seal(self, plaintext: bytes, aad: str) -> str:
        if self._pub is None:
            raise VaultError("chave pública do cofre não configurada")
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        ct = AESGCM(dek).encrypt(nonce, plaintext, aad.encode())
        blob = {
            "v": FORMAT,
            "kid": self._kid,
            "ek": _b64(self._pub.encrypt(dek, OAEP)),
            "n": _b64(nonce),
            "ct": _b64(ct),
        }
        return json.dumps(blob, separators=(",", ":"))

    def open(self, sealed: str, aad: str) -> bytes:
        blob = json.loads(sealed)
        priv = self._privs.get(blob.get("kid"))
        if priv is None:
            raise VaultError("chave privada do cofre indisponível neste serviço")
        try:
            dek = priv.decrypt(_unb64(blob["ek"]), OAEP)
            return AESGCM(dek).decrypt(_unb64(blob["n"]), _unb64(blob["ct"]), aad.encode())
        except Exception as e:  # noqa: BLE001 - não vazar detalhe criptográfico
            raise VaultError("segredo inválido ou adulterado") from e


def _read(path: str | None) -> bytes | None:
    if path and Path(path).is_file():
        return Path(path).read_bytes()
    return None


@lru_cache
def get_vault() -> Vault:
    s = get_settings()
    privs = [p for p in (_read(x.strip()) for x in (s.vault_private_keys or "").split(",") if x.strip()) if p]
    return Vault(_read(s.vault_public_key), privs)


def keygen(directory: str) -> None:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    priv_path, pub_path = d / "vault-private.pem", d / "vault-public.pem"
    if priv_path.exists():
        print(f"já existe: {priv_path}")
        return
    priv = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    priv_path.write_bytes(
        priv.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    os.chmod(priv_path, 0o600)
    pub_path.write_bytes(
        priv.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    print(f"chaves criadas em {d} (kid {key_id(pub_path.read_bytes())})")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "keygen":
        keygen(sys.argv[2])
    else:
        print("uso: python -m app.vault keygen <diretório>")
        sys.exit(2)
