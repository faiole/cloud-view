# Keycloak otimizado, servido em /auth atrás do Caddy, com o realm do Cloud View embutido.
FROM quay.io/keycloak/keycloak:26.1.4 AS builder
ENV KC_DB=postgres KC_HEALTH_ENABLED=true KC_HTTP_RELATIVE_PATH=/auth
RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:26.1.4
COPY --from=builder /opt/keycloak/ /opt/keycloak/
COPY realm-cloudview.json /opt/keycloak/data/import/realm-cloudview.json
COPY dev-seed.sh /opt/cloudview/dev-seed.sh
# --import-realm ignora o arquivo se o realm já existir (alterações posteriores via console/kcadm)
ENTRYPOINT ["/opt/keycloak/bin/kc.sh", "start", "--optimized", "--import-realm"]
