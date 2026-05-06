#!/usr/bin/env python3

from typing import Dict, List

import kopf
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException


GROUP = "cpu.example.com"
VERSION = "v1alpha1"
TOPOLOGY_PLURAL = "nodecputopologies"


def expand_cpuset(cpuset: str) -> List[int]:
    cpus: List[int] = []
    if not cpuset:
        return cpus

    for part in cpuset.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            start, end = part.split("-", 1)
            cpus.extend(range(int(start), int(end) + 1))
        else:
            cpus.append(int(part))

    return sorted(set(cpus))


def compress_cpuset(cpus: List[int]) -> str:
    if not cpus:
        return ""

    cpus = sorted(set(cpus))
    ranges = []
    start = prev = cpus[0]

    for cpu in cpus[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append((start, prev))
        start = prev = cpu

    ranges.append((start, prev))

    output = []
    for start, end in ranges:
        output.append(str(start) if start == end else f"{start}-{end}")

    return ",".join(output)


def split_cpuset_groups(cpuset: str) -> List[List[int]]:
    groups = []
    for part in cpuset.split(","):
        part = part.strip()
        if part:
            groups.append(expand_cpuset(part))
    return groups


def choose_balanced_reserved(cpulist: str, count: int) -> List[int]:
    """
    Choose count logical CPUs from a NUMA cpulist.

    For SMT-style cpulists such as '0-42,172-214', this spreads
    the reservation across the two ranges:
      count=40 -> 0-19,172-191
    """
    if count <= 0:
        return []

    groups = split_cpuset_groups(cpulist)
    if not groups:
        return []

    if len(groups) == 1:
        return groups[0][:count]

    per_group = count // len(groups)
    remainder = count % len(groups)

    reserved = []
    for idx, group in enumerate(groups):
        take = per_group + (1 if idx < remainder else 0)
        reserved.extend(group[:take])

    return sorted(reserved)


def compute_reserved_system_cpus(topologies: List[Dict], logical_cpus_per_numa: int) -> Dict[str, str]:
    result = {}

    for topo in topologies:
        status = topo.get("status", {})
        node_name = status.get("nodeName") or topo.get("spec", {}).get("nodeName")
        numa_nodes = status.get("numaNodes", [])

        if not node_name or not numa_nodes:
            continue

        reserved = []
        for numa in sorted(numa_nodes, key=lambda x: x.get("id", 0)):
            reserved.extend(choose_balanced_reserved(numa.get("cpus", ""), logical_cpus_per_numa))

        result[node_name] = compress_cpuset(reserved)

    return result


def list_node_topologies(namespace: str) -> List[Dict]:
    api = client.CustomObjectsApi()
    response = api.list_namespaced_custom_object(
        GROUP,
        VERSION,
        namespace,
        TOPOLOGY_PLURAL,
    )
    return response.get("items", [])


def upsert_configmap(namespace: str, name: str, data: Dict[str, str]):
    core = client.CoreV1Api()
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        data=data,
    )

    try:
        core.read_namespaced_config_map(name=name, namespace=namespace)
        core.patch_namespaced_config_map(name=name, namespace=namespace, body=body)
    except ApiException as e:
        if e.status == 404:
            core.create_namespaced_config_map(namespace=namespace, body=body)
        else:
            raise


def render_kubelet_config_example(
    node_reserved: Dict[str, str],
    cpu_manager_policy: str,
    topology_manager_policy: str,
) -> str:
    rendered = {
        "note": "Example only. Validate before applying to real OpenShift KubeletConfig.",
        "cpuManagerPolicy": cpu_manager_policy,
        "topologyManagerPolicy": topology_manager_policy,
        "perNodeReservedSystemCPUs": node_reserved,
    }
    return yaml.safe_dump(rendered, sort_keys=False)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    config.load_incluster_config()
    # Do not set settings.posting.level as a string.
    # Kopf expects an integer logging level. Leaving default is safest for this PoC.


@kopf.on.create(GROUP, VERSION, "cpuplacementpolicies")
@kopf.on.update(GROUP, VERSION, "cpuplacementpolicies")
def reconcile_cpu_placement_policy(spec, name, namespace, logger, **_):
    logical_per_numa = spec.get("systemReserved", {}).get("logicalCPUsPerNuma", 0)
    cpu_manager_policy = spec.get("cpuManagerPolicy", "static")
    topology_manager_policy = spec.get("topologyManagerPolicy", "restricted")

    topologies = list_node_topologies(namespace)
    node_reserved = compute_reserved_system_cpus(topologies, logical_per_numa)

    data = {
        "policyName": name,
        "logicalCPUsPerNuma": str(logical_per_numa),
        "reservedSystemCPUsByNode.yaml": yaml.safe_dump(node_reserved, sort_keys=True),
        "kubeletConfigExample.yaml": render_kubelet_config_example(
            node_reserved=node_reserved,
            cpu_manager_policy=cpu_manager_policy,
            topology_manager_policy=topology_manager_policy,
        ),
    }

    upsert_configmap(
        namespace=namespace,
        name=f"{name}-computed-cpu-policy",
        data=data,
    )

    logger.info(
        "Reconciled CPUPlacementPolicy %s; computed reserved CPUs for %d nodes",
        name,
        len(node_reserved),
    )


@kopf.on.create(GROUP, VERSION, "nodecputopologies")
@kopf.on.update(GROUP, VERSION, "nodecputopologies")
def on_node_topology_change(namespace, logger, **_):
    logger.info(
        "NodeCPUTopology changed in namespace %s. Re-apply or update CPUPlacementPolicy to recompute.",
        namespace,
    )
