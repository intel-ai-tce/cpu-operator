#!/usr/bin/env python3

import glob
import hashlib
import json
import os
import re
import time
from typing import Dict, List

from kubernetes import client, config
from kubernetes.client.rest import ApiException


GROUP = "cpu.example.com"
VERSION = "v1alpha1"
PLURAL = "nodecputopologies"


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def parse_meminfo(path: str) -> int:
    """Return NUMA node memory in MiB from nodeX/meminfo."""
    try:
        text = read_text(path)
    except FileNotFoundError:
        return 0

    match = re.search(r"MemTotal:\s+(\d+)\s+kB", text)
    if not match:
        return 0
    return int(match.group(1)) // 1024


def read_numa_nodes(sys_node_path: str = "/host-sys/devices/system/node") -> List[Dict]:
    numa_nodes = []

    for node_path in sorted(glob.glob(f"{sys_node_path}/node[0-9]*")):
        node_id = int(os.path.basename(node_path).replace("node", ""))
        cpulist_path = os.path.join(node_path, "cpulist")
        meminfo_path = os.path.join(node_path, "meminfo")

        try:
            cpulist = read_text(cpulist_path)
        except FileNotFoundError:
            continue

        numa_nodes.append(
            {
                "id": node_id,
                "cpus": cpulist,
                "memoryMiB": parse_meminfo(meminfo_path),
            }
        )

    return numa_nodes


def read_thread_siblings(sys_cpu_path: str = "/host-sys/devices/system/cpu") -> Dict[str, str]:
    siblings = {}

    for topo_path in glob.glob(f"{sys_cpu_path}/cpu[0-9]*/topology/thread_siblings_list"):
        cpu_dir = topo_path.split("/")[-3]
        cpu_id = cpu_dir.replace("cpu", "")

        try:
            siblings[cpu_id] = read_text(topo_path)
        except FileNotFoundError:
            continue

    return siblings


def read_cpu_online(sys_cpu_path: str = "/host-sys/devices/system/cpu") -> str:
    try:
        return read_text(f"{sys_cpu_path}/online")
    except FileNotFoundError:
        return ""


def topology_hash(payload: Dict) -> str:
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_object(api: client.CustomObjectsApi, namespace: str, name: str, node_name: str):
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "NodeCPUTopology",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "nodeName": node_name,
        },
    }

    try:
        api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
    except ApiException as e:
        if e.status == 404:
            api.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, body)
        else:
            raise


def patch_status(api: client.CustomObjectsApi, namespace: str, name: str, status: Dict):
    api.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        namespace,
        PLURAL,
        name,
        {"status": status},
    )


def main():
    namespace = os.environ.get("NAMESPACE", "cpu-operator-system")
    node_name = os.environ["NODE_NAME"]
    object_name = node_name.lower().replace("_", "-")
    interval_seconds = int(os.environ.get("INTERVAL_SECONDS", "60"))

    config.load_incluster_config()
    api = client.CustomObjectsApi()
    last_hash = None

    while True:
        numa_nodes = read_numa_nodes()
        thread_siblings = read_thread_siblings()
        online_cpus = read_cpu_online()

        status = {
            "nodeName": node_name,
            "topologyReady": bool(numa_nodes),
            "onlineCPUs": online_cpus,
            "numaNodes": numa_nodes,
            "threadSiblings": thread_siblings,
            "conditions": [
                {
                    "type": "TopologyReady",
                    "status": "True" if numa_nodes else "False",
                    "reason": "TopologyDiscovered" if numa_nodes else "NoNUMATopologyFound",
                }
            ],
        }
        status["topologyHash"] = topology_hash(status)

        try:
            ensure_object(api, namespace, object_name, node_name)

            if status["topologyHash"] != last_hash:
                patch_status(api, namespace, object_name, status)
                print(f"Updated NodeCPUTopology/{object_name}: {status['topologyHash']}", flush=True)
                last_hash = status["topologyHash"]
            else:
                print(f"No topology change for {node_name}", flush=True)

        except Exception as exc:
            print(f"ERROR: failed to publish topology for {node_name}: {exc}", flush=True)

        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
