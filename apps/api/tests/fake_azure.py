"""Azure falso para testes: responde às APIs REST usadas pelo coletor."""

import gzip
import json
from urllib.parse import parse_qs, urlparse

import httpx

TENANT = "11111111-2222-3333-4444-555555555555"
SUB_A = "aaaaaaaa-0000-0000-0000-000000000001"  # empresa A inteira
SUB_S = "bbbbbbbb-0000-0000-0000-000000000002"  # compartilhada por tag
RG = f"/subscriptions/{SUB_A}/resourceGroups/rg-erp"
VM_ID = f"{RG}/providers/Microsoft.Compute/virtualMachines/erp-vm"
VM_B = f"/subscriptions/{SUB_S}/resourceGroups/rg-b/providers/Microsoft.Compute/virtualMachines/site-b"
NIC = f"{RG}/providers/Microsoft.Network/networkInterfaces/erp-nic"
PIP = f"{RG}/providers/Microsoft.Network/publicIPAddresses/erp-ip"
PIP_FREE = f"{RG}/providers/Microsoft.Network/publicIPAddresses/old-ip"
NSG = f"{RG}/providers/Microsoft.Network/networkSecurityGroups/erp-nsg"
VNET = f"{RG}/providers/Microsoft.Network/virtualNetworks/vnet-prod"
SUBNET = f"{VNET}/subnets/app"
DISK_FREE = f"{RG}/providers/Microsoft.Compute/disks/old-disk"


class FakeAzure:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.token_forms: list[dict] = []
        self.throttle_cost_once = True
        self.vm_power = "PowerState/running"
        self.resource_health = "Available"
        self.cost_rows = {
            "ActualCost": [[100.0, 20261005, "Virtual Machines", "brazilsouth", "USD"]],
            "AmortizedCost": [[80.0, 20261005, "Virtual Machines", "brazilsouth", "USD"]],
        }
        self.advisor = [
            {
                "id": f"{VM_ID}/providers/Microsoft.Advisor/recommendations/rec-1",
                "properties": {
                    "impact": "High",
                    "impactedField": "Microsoft.Compute/virtualMachines",
                    "impactedValue": "erp-vm",
                    "shortDescription": {
                        "problem": "Underutilized VM",
                        "solution": "Right-size or shutdown underutilized virtual machines",
                    },
                    "extendedProperties": {
                        "savingsAmount": "61.5",
                        "savingsCurrency": "USD",
                        "currentSku": "Standard_D8s_v5",
                        "targetSku": "Standard_D4s_v5",
                    },
                    "resourceMetadata": {"resourceId": VM_ID},
                },
            }
        ]
        self.invoices = []
        self.blobs: dict[str, tuple[bytes, str]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        self.calls.append((request.method, f"{url.host}{path}"))
        if url.host == "login.microsoftonline.com":
            form = parse_qs(request.content.decode())
            self.token_forms.append({k: v[0] for k, v in form.items()})
            return httpx.Response(200, json={"access_token": f"tok-{path.split('/')[1]}", "expires_in": 3600})
        if url.host == "invoices.blob.core.windows.net":
            return httpx.Response(200, content=b"%PDF-1.7 fatura azure")
        if url.host.endswith(".blob.core.windows.net"):
            return self._blob(request)
        if path == "/subscriptions":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"subscriptionId": SUB_A, "displayName": "empresa-a-prod", "state": "Enabled"},
                        {"subscriptionId": SUB_S, "displayName": "compartilhada", "state": "Enabled"},
                    ]
                },
            )
        if path == "/providers/Microsoft.ResourceGraph/resources":
            return self._graph(json.loads(request.content))
        if path.endswith("/vmSizes"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"name": "Standard_D4s_v5", "numberOfCores": 4, "memoryInMB": 16384},
                        {"name": "Standard_B2s", "numberOfCores": 2, "memoryInMB": 4096},
                    ]
                },
            )
        if path.endswith("/availabilityStatuses"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": f"{VM_ID}/providers/Microsoft.ResourceHealth/availabilityStatuses/current",
                            "properties": {
                                "availabilityState": self.resource_health,
                                "summary": "Host com falha de hardware",
                                "reasonType": "Unplanned",
                            },
                        }
                    ]
                    if SUB_A in path
                    else []
                },
            )
        if path.endswith("/Microsoft.ResourceHealth/events"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "TRK-123",
                            "properties": {
                                "eventType": "PlannedMaintenance",
                                "status": "Active",
                                "title": "Manutenção planejada em Brazil South",
                                "summary": "Reinício das VMs",
                                "impactStartTime": "2026-11-05T03:00:00Z",
                                "impact": [
                                    {
                                        "impactedService": "Virtual Machines",
                                        "impactedRegions": [{"impactedRegion": "Brazil South"}],
                                    }
                                ],
                            },
                        }
                    ]
                    if SUB_A in path
                    else []
                },
            )
        if path.endswith("/Microsoft.CostManagement/query"):
            if self.throttle_cost_once:
                self.throttle_cost_once = False
                return httpx.Response(
                    429, headers={"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "1"}, json={}
                )
            body = json.loads(request.content)
            rows = self.cost_rows[body["type"]] if SUB_A in path else []
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "columns": [
                            {"name": "Cost"},
                            {"name": "UsageDate"},
                            {"name": "ServiceName"},
                            {"name": "ResourceLocation"},
                            {"name": "Currency"},
                        ],
                        "rows": rows,
                    }
                },
            )
        if path.endswith("/Microsoft.Advisor/recommendations"):
            return httpx.Response(200, json={"value": self.advisor if SUB_A in path else []})
        if path.lower().startswith(VM_ID.lower()):
            if request.method == "POST":
                action = path.rsplit("/", 1)[-1]
                self.vm_power = {
                    "deallocate": "PowerState/deallocated",
                    "start": "PowerState/running",
                    "restart": "PowerState/running",
                }[action]
                return httpx.Response(202)
            if path.endswith("/instanceView"):
                return httpx.Response(
                    200, json={"statuses": [{"code": "ProvisioningState/succeeded"}, {"code": self.vm_power}]}
                )
        if "/Microsoft.Billing/billingAccounts/" in path:
            if path.endswith("/invoices"):
                return httpx.Response(200, json={"value": self.invoices})
            if path.endswith("/download"):
                return httpx.Response(
                    202,
                    headers={
                        "Location": "https://management.azure.com/operationResults/op-1",
                        "Retry-After": "0",
                    },
                )
        if path == "/operationResults/op-1":
            return httpx.Response(
                200, json={"url": "https://invoices.blob.core.windows.net/x/fatura.pdf?sig=1"}
            )
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": path}})

    def _graph(self, body):
        q = body["query"].lower()
        subs = body["subscriptions"]
        a = SUB_A in subs

        def resp(rows):
            return httpx.Response(200, json={"data": rows})

        if "microsoft.compute/virtualmachines" in q:
            rows = []
            if a:
                rows.append(
                    {
                        "id": VM_ID,
                        "name": "erp-vm",
                        "location": "brazilsouth",
                        "resourceGroup": "rg-erp",
                        "tags": {"cloudview-actions": "enabled"},
                        "vmSize": "Standard_D4s_v5",
                        "osType": "Linux",
                        "powerState": self.vm_power,
                        "nics": [{"id": NIC}],
                    }
                )
            else:
                rows.append(
                    {
                        "id": VM_B,
                        "name": "site-b",
                        "location": "eastus",
                        "resourceGroup": "rg-b",
                        "tags": {"cost-center": "empresa-b"},
                        "vmSize": "Standard_B2s",
                        "osType": "Windows",
                        "powerState": "PowerState/stopped",
                        "nics": [],
                    }
                )
            return resp(rows)
        if "microsoft.network/networkinterfaces" in q:
            return resp(
                [
                    {
                        "id": NIC,
                        "nsg": NSG,
                        "vm": VM_ID,
                        "ipConfigurations": [
                            {
                                "properties": {
                                    "privateIPAddress": "10.0.1.4",
                                    "publicIPAddress": {"id": PIP},
                                    "subnet": {"id": SUBNET},
                                }
                            }
                        ],
                    }
                ]
                if a
                else []
            )
        if "microsoft.network/publicipaddresses" in q:
            return resp(
                [
                    {
                        "id": PIP,
                        "name": "erp-ip",
                        "location": "brazilsouth",
                        "ip": "20.1.2.3",
                        "ipConfig": NIC + "/ipConfigurations/ip1",
                        "sku": "Standard",
                    },
                    {
                        "id": PIP_FREE,
                        "name": "old-ip",
                        "location": "brazilsouth",
                        "ip": "20.9.9.9",
                        "ipConfig": "",
                        "sku": "Basic",
                    },
                ]
                if a
                else []
            )
        if "microsoft.compute/disks" in q:
            return resp(
                [
                    {
                        "id": DISK_FREE,
                        "name": "old-disk",
                        "location": "brazilsouth",
                        "sizeGb": 128,
                        "state": "Unattached",
                        "sku": "Premium_LRS",
                        "managedBy": "",
                    }
                ]
                if a
                else []
            )
        if "microsoft.network/virtualnetworks" in q:
            return resp(
                [
                    {
                        "id": VNET,
                        "name": "vnet-prod",
                        "location": "brazilsouth",
                        "prefixes": ["10.0.0.0/16"],
                        "subnets": [
                            {"id": SUBNET, "name": "app", "properties": {"addressPrefix": "10.0.1.0/24"}}
                        ],
                    }
                ]
                if a
                else []
            )
        if "microsoft.network/networksecuritygroups" in q:
            return resp(
                [
                    {
                        "id": NSG,
                        "name": "erp-nsg",
                        "location": "brazilsouth",
                        "rules": [
                            {
                                "name": "ssh",
                                "properties": {
                                    "direction": "Inbound",
                                    "access": "Allow",
                                    "sourceAddressPrefix": "Internet",
                                    "destinationPortRange": "22",
                                    "priority": 100,
                                },
                            },
                            {
                                "name": "https",
                                "properties": {
                                    "direction": "Inbound",
                                    "access": "Allow",
                                    "sourceAddressPrefix": "*",
                                    "destinationPortRange": "443",
                                    "priority": 110,
                                },
                            },
                            {
                                "name": "rdp-office",
                                "properties": {
                                    "direction": "Inbound",
                                    "access": "Allow",
                                    "sourceAddressPrefix": "200.1.2.0/24",
                                    "destinationPortRange": "3389",
                                    "priority": 120,
                                },
                            },
                        ],
                    }
                ]
                if a
                else []
            )
        return resp([])

    def _blob(self, request):
        q = parse_qs(urlparse(str(request.url)).query)
        if q.get("comp") == ["list"]:
            prefix = q.get("prefix", [""])[0]
            items = "".join(
                f"<Blob><Name>{name}</Name><Properties><Last-Modified>{mod}</Last-Modified></Properties></Blob>"
                for name, (_, mod) in self.blobs.items()
                if name.startswith(prefix)
            )
            return httpx.Response(
                200,
                content=f"<?xml version='1.0'?><EnumerationResults><Blobs>{items}</Blobs><NextMarker/></EnumerationResults>".encode(),
            )
        name = request.url.path.split("/", 2)[2]
        data = self.blobs.get(name)
        return httpx.Response(200, content=data[0]) if data else httpx.Response(404)


def gz(data: bytes) -> bytes:
    return gzip.compress(data)
