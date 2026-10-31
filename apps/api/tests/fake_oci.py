"""OCI falsa para testes: confere a assinatura HTTP de cada requisição e responde às APIs usadas."""

import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

TENANCY = "ocid1.tenancy.oc1..aaaaaaaacliente000000000000000000000000"
COMP_A = "ocid1.compartment.oc1..aaaaaaaaempresaa0000000000000000000000"
COMP_S = "ocid1.compartment.oc1..aaaaaaaacompartilhado00000000000000000"
REGION = "sa-saopaulo-1"
INST_A = "ocid1.instance.oc1.sa-saopaulo-1.erpapp"
INST_B = "ocid1.instance.oc1.sa-saopaulo-1.siteb"
VCN = "ocid1.vcn.oc1.sa-saopaulo-1.prod"
SUBNET = "ocid1.subnet.oc1.sa-saopaulo-1.app"
SECLIST = "ocid1.securitylist.oc1.sa-saopaulo-1.default"
NSG = "ocid1.networksecuritygroup.oc1.sa-saopaulo-1.web"
VNIC = "ocid1.vnic.oc1.sa-saopaulo-1.erpvnic"
VOL_FREE = "ocid1.volume.oc1.sa-saopaulo-1.old"
VOL_USED = "ocid1.volume.oc1.sa-saopaulo-1.data"


class FakeOci:
    def __init__(self):
        self.keys: dict[str, object] = {}
        self.calls: list[tuple[str, str]] = []
        self.inst_a_state = "RUNNING"
        self.objects: dict[str, bytes] = {}
        self.usage_items = [
            {
                "computedAmount": 42.5,
                "currency": "BRL",
                "service": "Compute",
                "compartmentId": COMP_A,
                "compartmentName": "empresa-a",
                "region": REGION,
                "timeUsageStarted": "2026-10-05T00:00:00.000Z",
            },
        ]
        self.resource_actions = [
            {
                "id": "ocid1.optimizerresourceaction.oc1..ra1",
                "recommendationId": "rec-1",
                "resourceId": INST_A,
                "name": "erp-app",
                "resourceType": "Instance",
                "compartmentId": COMP_A,
                "estimatedCostSaving": 120.4,
                "status": "PENDING",
                "action": {"type": "KB_ARTICLE", "description": "Downsize to VM.Standard.E4.Flex 1 OCPU"},
                "extendedMetadata": {
                    "currentShape": "VM.Standard.E4.Flex",
                    "recommendedShape": "VM.Standard.E4.Flex (1 OCPU)",
                },
            },
        ]

    def register(self, key_id: str, public_pem: str) -> None:
        self.keys[key_id] = serialization.load_pem_public_key(public_pem.encode())

    def _verify(self, request: httpx.Request) -> str | None:
        auth = request.headers.get("authorization", "")
        params = dict(re.findall(r'(\w+)="([^"]*)"', auth))
        key = self.keys.get(params.get("keyId"))
        if key is None:
            return "unknown key"
        parts = urlsplit(str(request.url))
        lines = []
        for h in params["headers"].split():
            if h == "(request-target)":
                lines.append(
                    f"(request-target): {request.method.lower()} {parts.path}{'?' + parts.query if parts.query else ''}"
                )
            else:
                lines.append(f"{h}: {request.headers[h]}")
        if request.method == "POST":
            body_hash = base64.b64encode(hashlib.sha256(request.content).digest()).decode()
            if (
                request.headers.get("x-content-sha256") != body_hash
                or "x-content-sha256" not in params["headers"]
            ):
                return "body hash"
        try:
            key.verify(
                base64.b64decode(params["signature"]),
                "\n".join(lines).encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:  # noqa: BLE001
            return "bad signature"
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        err = self._verify(request)
        if err:
            return httpx.Response(401, json={"code": "NotAuthenticated", "message": err})
        u = urlsplit(str(request.url))
        service = u.hostname.split(".")[0]
        path = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((request.method, f"{service}{path}"))
        handler = getattr(self, f"_{service}", None)
        return handler(request, path, q) if handler else httpx.Response(404, json={"code": "NotFound"})

    # --- serviços -------------------------------------------------------------------------------

    def _identity(self, request, path, q):
        if path.endswith("/regionSubscriptions"):
            return httpx.Response(200, json=[{"regionName": REGION, "isHomeRegion": True, "status": "READY"}])
        if path == f"/20160918/tenancies/{TENANCY}":
            return httpx.Response(200, json={"id": TENANCY, "name": "cliente"})
        if path == "/20160918/compartments":
            if q.get("page") == "2":
                return httpx.Response(
                    200, json=[{"id": COMP_S, "name": "compartilhado", "lifecycleState": "ACTIVE"}]
                )
            return httpx.Response(
                200,
                json=[{"id": COMP_A, "name": "empresa-a", "lifecycleState": "ACTIVE"}],
                headers={"opc-next-page": "2"},
            )
        return httpx.Response(404, json={"code": "NotFound"})

    def _iaas(self, request, path, q):
        comp = q.get("compartmentId")
        a = comp == COMP_A
        p = path.replace("/20160918", "")
        if request.method == "POST" and p == f"/instances/{INST_A}":
            self.inst_a_state = {"SOFTSTOP": "STOPPING", "START": "STARTING", "SOFTRESET": "RUNNING"}[
                q["action"]
            ]
            return httpx.Response(200, json={"id": INST_A, "lifecycleState": self.inst_a_state})
        data = {
            "/securityLists": [
                {
                    "id": SECLIST,
                    "displayName": "Default Security List",
                    "vcnId": VCN,
                    "lifecycleState": "AVAILABLE",
                    "ingressSecurityRules": [
                        {
                            "source": "0.0.0.0/0",
                            "protocol": "6",
                            "tcpOptions": {"destinationPortRange": {"min": 22, "max": 22}},
                        },
                        {"source": "10.0.0.0/16", "protocol": "all"},
                    ],
                }
            ]
            if a
            else [],
            "/networkSecurityGroups": [
                {"id": NSG, "displayName": "web", "vcnId": VCN, "lifecycleState": "AVAILABLE"}
            ]
            if a
            else [],
            f"/networkSecurityGroups/{NSG}/securityRules": [
                {
                    "direction": "INGRESS",
                    "source": "0.0.0.0/0",
                    "protocol": "6",
                    "tcpOptions": {"destinationPortRange": {"min": 443, "max": 443}},
                },
                {
                    "direction": "INGRESS",
                    "source": "0.0.0.0/0",
                    "protocol": "6",
                    "tcpOptions": {"destinationPortRange": {"min": 3389, "max": 3389}},
                },
            ],
            "/vcns": [
                {
                    "id": VCN,
                    "displayName": "vcn-prod",
                    "cidrBlocks": ["10.0.0.0/16"],
                    "lifecycleState": "AVAILABLE",
                }
            ]
            if a
            else [],
            "/subnets": [
                {
                    "id": SUBNET,
                    "displayName": "app",
                    "vcnId": VCN,
                    "cidrBlock": "10.0.1.0/24",
                    "lifecycleState": "AVAILABLE",
                    "securityListIds": [SECLIST],
                    "prohibitPublicIpOnVnic": False,
                }
            ]
            if a
            else [],
            "/vnicAttachments": [{"instanceId": INST_A, "vnicId": VNIC, "lifecycleState": "ATTACHED"}]
            if a
            else [],
            f"/vnics/{VNIC}": {
                "id": VNIC,
                "privateIp": "10.0.1.10",
                "publicIp": "150.1.2.3",
                "subnetId": SUBNET,
                "nsgIds": [NSG],
            },
            "/instances": (
                [
                    {
                        "id": INST_A,
                        "displayName": "erp-app",
                        "lifecycleState": self.inst_a_state,
                        "shape": "VM.Standard.E4.Flex",
                        "shapeConfig": {"ocpus": 2, "memoryInGBs": 32, "vcpus": 4},
                        "availabilityDomain": "AD-1",
                        "freeformTags": {"cloudview-actions": "enabled"},
                        "sourceDetails": {"sourceType": "image"},
                    }
                ]
                if a
                else [
                    {
                        "id": INST_B,
                        "displayName": "site-b",
                        "lifecycleState": "RUNNING",
                        "shape": "VM.Standard2.1",
                        "shapeConfig": {"ocpus": 1, "memoryInGBs": 15},
                        "freeformTags": {"cost-center": "empresa-b"},
                        "definedTags": {"Operations": {"owner": "time-b"}},
                    }
                ]
            ),
            "/volumeAttachments": [{"volumeId": VOL_USED, "lifecycleState": "ATTACHED"}] if a else [],
            "/volumes": [
                {"id": VOL_FREE, "displayName": "old", "sizeInGBs": 200, "lifecycleState": "AVAILABLE"},
                {"id": VOL_USED, "displayName": "data", "sizeInGBs": 100, "lifecycleState": "AVAILABLE"},
            ]
            if a
            else [],
            "/publicIps": [
                {"id": "ocid1.publicip.oc1..free", "ipAddress": "150.9.9.9", "lifetime": "RESERVED"}
            ]
            if a
            else [],
            "/instanceMaintenanceEvents": [
                {
                    "id": "ocid1.instancemaintenanceevent.oc1..m1",
                    "instanceId": INST_A,
                    "lifecycleState": "SCHEDULED",
                    "instanceAction": "REBOOT_MIGRATION",
                    "description": "Manutenção do host",
                    "timeWindowStart": "2026-11-10T02:00:00Z",
                }
            ]
            if a
            else [],
        }
        if p in data:
            return httpx.Response(200, json=data[p])
        return httpx.Response(404, json={"code": "NotAuthorizedOrNotFound"})

    def _announcements(self, request, path, q):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "ocid1.announcement.oc1..a1",
                        "announcementType": "SCHEDULED_MAINTENANCE",
                        "summary": "Manutenção do Object Storage em São Paulo",
                        "services": ["Object Storage"],
                        "affectedRegions": [REGION],
                        "timeOneValue": "2026-11-12T03:00:00Z",
                    }
                ]
            },
        )

    def _usageapi(self, request, path, q):
        body = json.loads(request.content)
        assert body["tenantId"] == TENANCY and body["granularity"] == "DAILY"
        return httpx.Response(200, json={"items": self.usage_items})

    def _optimizer(self, request, path, q):
        if path.endswith("/recommendations"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "rec-1",
                            "name": "compute-rightsizing",
                            "description": "Right-size underutilized instances",
                        }
                    ]
                },
            )
        if path.endswith("/resourceActions"):
            return httpx.Response(200, json={"items": self.resource_actions})
        return httpx.Response(404, json={"code": "NotFound"})

    def _objectstorage(self, request, path, q):
        base = f"/n/bling/b/{TENANCY}/o"
        if path == base:
            names = [n for n in self.objects if n.startswith(q.get("prefix", ""))]
            return httpx.Response(200, json={"objects": [{"name": n} for n in names]})
        name = unquote(path[len(base) + 1 :])
        return (
            httpx.Response(200, content=self.objects[name]) if name in self.objects else httpx.Response(404)
        )
