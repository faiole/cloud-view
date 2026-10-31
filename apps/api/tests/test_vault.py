import base64
import os

import pytest

from app.core import db as dbmod
from app.services.secrets import read_secret, store_secret
from app.services.storage import EncryptedLocalStorage
from app.vault import Vault, VaultError, keygen


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("vault")
    keygen(str(d))
    return (d / "vault-public.pem").read_bytes(), (d / "vault-private.pem").read_bytes()


def test_api_can_seal_but_not_open(keys):
    pub, priv = keys
    api = Vault(pub)
    worker = Vault(pub, [priv])
    sealed = api.seal(b"oci-private-key", aad="cloud_secret:1")
    assert b"oci-private-key" not in sealed.encode()
    with pytest.raises(VaultError):
        api.open(sealed, aad="cloud_secret:1")
    assert worker.open(sealed, aad="cloud_secret:1") == b"oci-private-key"


def test_secret_bound_to_its_id(keys):
    pub, priv = keys
    v = Vault(pub, [priv])
    sealed = v.seal(b"x", aad="cloud_secret:1")
    with pytest.raises(VaultError):
        v.open(sealed, aad="cloud_secret:2")


def test_key_rotation_keeps_old_secrets_readable(keys, tmp_path):
    pub, priv = keys
    keygen(str(tmp_path))
    new_pub, new_priv = (
        (tmp_path / "vault-public.pem").read_bytes(),
        (tmp_path / "vault-private.pem").read_bytes(),
    )
    old = Vault(pub).seal(b"antigo", aad="a")
    worker = Vault(new_pub, [new_priv, priv])
    assert worker.open(old, aad="a") == b"antigo"
    assert worker.open(worker.seal(b"novo", aad="b"), aad="b") == b"novo"


def test_store_and_read_through_database(client, keys):
    pub, priv = keys
    with dbmod.get_sessionmaker()() as db:
        dbmod.apply_tenant_scope(db, tenant_ids=[], is_admin=True)
        sid = store_secret(db, Vault(pub), "oci_api_key", b"-----BEGIN PRIVATE KEY-----", "adm@msp.com")
        db.commit()
    with dbmod.get_sessionmaker()() as db:
        with pytest.raises(VaultError):
            read_secret(db, Vault(pub), sid)
        assert read_secret(db, Vault(pub, [priv]), sid) == b"-----BEGIN PRIVATE KEY-----"


def test_documents_encrypted_at_rest(tmp_path):
    st = EncryptedLocalStorage(str(tmp_path), base64.b64encode(os.urandom(32)).decode())
    st.put("tenants/a/f.pdf", b"%PDF-conteudo", "application/pdf")
    raw = (tmp_path / "tenants/a/f.pdf").read_bytes()
    assert b"%PDF" not in raw
    assert st.read("tenants/a/f.pdf") == b"%PDF-conteudo"
    assert st.download("tenants/a/f.pdf", "f.pdf", "application/pdf").body == b"%PDF-conteudo"
