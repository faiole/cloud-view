"""Tipos de recurso normalizados entre nuvens."""

INSTANCE_TYPES = ("ec2_instance", "vm", "oci_instance")  # AWS EC2, Azure VM, OCI Compute
VOLUME_TYPES = ("ebs_volume", "disk", "block_volume")
PUBLIC_IP_TYPES = ("elastic_ip", "public_ip")
NETWORK_TYPES = ("vpc", "subnet", "security_group")  # VPC/VNet, subnet, SG/NSG

ACTION_TAGS = ("cloudview-actions", "cloudview.actions")  # tag livre ou tag definida (OCI) que libera ações

# Estados de energia equivalentes para as ações rápidas
RUNNING = {"running"}
STOPPED = {"stopped", "deallocated"}
