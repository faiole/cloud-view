"""Tipos de recurso normalizados entre nuvens."""

INSTANCE_TYPES = ("ec2_instance", "vm")  # AWS EC2, Azure VM
VOLUME_TYPES = ("ebs_volume", "disk")
PUBLIC_IP_TYPES = ("elastic_ip", "public_ip")
NETWORK_TYPES = ("vpc", "subnet", "security_group")  # VPC/VNet, subnet, SG/NSG

# Estados de energia equivalentes para as ações rápidas
RUNNING = {"running"}
STOPPED = {"stopped", "deallocated"}
