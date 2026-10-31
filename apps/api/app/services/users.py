from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppUser
from app.services.keycloak_admin import KeycloakAdmin, KeycloakAdminError


def ensure_user(db: Session, kc: KeycloakAdmin, email: str, name: str | None) -> tuple[AppUser, bool | None]:
    """Garante que o usuário existe no painel e no Keycloak.

    Retorna (usuário, e-mail_de_convite_enviado). O segundo valor é None quando o usuário já existia
    no Keycloak (não há convite a enviar) e False quando o e-mail falhou — o acesso é concedido mesmo
    assim e o administrador pode reenviar.
    """
    email = email.lower()
    user = db.scalar(select(AppUser).where(AppUser.email == email))
    if user is None:
        user = AppUser(email=email, name=name)
        db.add(user)
        db.flush()
    if user.keycloak_sub:
        return user, None

    kc_id = kc.find_user_id(email)
    sent: bool | None = None
    if kc_id is None:
        first, _, last = (name or "").partition(" ")
        kc_id = kc.create_user(email, first or None, last or None)
        try:
            kc.send_setup_email(kc_id)
            sent = True
        except KeycloakAdminError:
            sent = False
        user.invited_at = datetime.now(UTC)
    user.keycloak_sub = kc_id
    db.flush()
    return user, sent
