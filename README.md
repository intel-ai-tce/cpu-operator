# CPU Operator Demo

This package contains a minimal **CPU Operator** and **Node topology agent** example for Kubernetes/OpenShift.

The design intentionally avoids NRI and treats DRA as optional.

## Architecture

```text
Node topology agent DaemonSet
  -> runs on each worker node
  -> reads /sys CPU and NUMA topology
  -> writes NodeCPUTopology.status

CPU Operator
  -> runs as a Deployment in cpu-operator-system
  -> watches NodeCPUTopology and CPUPlacementPolicy
  -> computes NUMA-balanced reservedSystemCPUs
  -> writes a ConfigMap with the computed policy

Kubelet CPU Manager / Topology Manager
  -> not modified directly by this demo
  -> production operator would generate OpenShift KubeletConfig or PerformanceProfile
```

## Files

```text
.
├── crds.yaml
├── rbac.yaml
├── agent/
│   ├── agent.py
│   ├── Dockerfile
│   └── daemonset.yaml
├── operator/
│   ├── operator.py
│   ├── Dockerfile
│   └── deployment.yaml
├── examples/
│   ├── cpu-placement-policy.yaml
│   └── fake-node-topology.yaml
└── scripts/
    ├── build-and-push.sh
    ├── deploy.sh
    └── cleanup.sh
```

## Prerequisites

You need:

- `oc` or `kubectl`
- `podman` or compatible container builder
- access to a container registry
- cluster-admin or equivalent permissions for CRDs/RBAC
- OpenShift SCC permission if using the real DaemonSet with `/sys` hostPath

## Quick start on OpenShift

### 1. Set registry

```bash
export REGISTRY=quay.io/YOUR_ORG
```

Edit image names in:

```text
agent/daemonset.yaml
operator/deployment.yaml
```

Replace:

```text
quay.io/YOUR_ORG
```

with your real registry path.

### 2. Build and push images

```bash
chmod +x scripts/*.sh
REGISTRY=$REGISTRY ./scripts/build-and-push.sh
```

### 3. Apply CRDs and RBAC

```bash
oc apply -f crds.yaml
oc apply -f rbac.yaml
```

### 4. Allow hostPath access for the topology agent on OpenShift

The agent mounts `/sys` read-only. On OpenShift, the default SCC may block this.

For a lab cluster:

```bash
oc adm policy add-scc-to-user privileged \
  -z node-topology-agent \
  -n cpu-operator-system
```

### 5. Deploy components

```bash
oc apply -f agent/daemonset.yaml
oc apply -f operator/deployment.yaml
```

Check pods:

```bash
oc get pods -n cpu-operator-system -o wide
oc logs -n cpu-operator-system ds/node-topology-agent
oc logs -n cpu-operator-system deploy/cpu-operator
```

### 6. Verify topology objects

```bash
oc get nodecputopologies -n cpu-operator-system
oc get nodecputopologies -n cpu-operator-system -o yaml
```

### 7. Apply CPUPlacementPolicy

```bash
oc apply -f examples/cpu-placement-policy.yaml
```

Check computed policy:

```bash
oc get cm vllm-cpu-policy-computed-cpu-policy \
  -n cpu-operator-system \
  -o yaml
```

For a topology like:

```text
NUMA node0: 0-42,172-214
NUMA node1: 43-85,215-257
NUMA node2: 86-128,258-300
NUMA node3: 129-171,301-343
```

With:

```yaml
systemReserved:
  logicalCPUsPerNuma: 40
```

The computed result should look similar to:

```text
0-19,43-62,86-105,129-148,172-191,215-234,258-277,301-320
```

## Test without deploying the DaemonSet

You can test the operator logic with fake topology.

```bash
oc apply -f crds.yaml
oc apply -f rbac.yaml
oc apply -f operator/deployment.yaml

oc apply -f examples/fake-node-topology.yaml
oc apply -f examples/cpu-placement-policy.yaml

oc get cm vllm-cpu-policy-computed-cpu-policy \
  -n cpu-operator-system \
  -o yaml
```

## Important limitation

This demo does **not** patch OpenShift `KubeletConfig` directly. It only writes an example computed ConfigMap.

A production CPU Operator would create/patch objects such as:

```yaml
apiVersion: machineconfiguration.openshift.io/v1
kind: KubeletConfig
metadata:
  name: inference-cpu-manager
spec:
  machineConfigPoolSelector:
    matchLabels:
      pools.operator.machineconfiguration.openshift.io/inference: ""
  kubeletConfig:
    cpuManagerPolicy: static
    topologyManagerPolicy: restricted
    topologyManagerScope: pod
    reservedSystemCPUs: "0-19,43-62,86-105,129-148,172-191,215-234,258-277,301-320"
```

Be careful: applying `KubeletConfig` can trigger MachineConfig rollout and node reboot.

## Component ownership

| Component | Responsibility | Where it runs |
|---|---|---|
| CPU Operator | Policy automation, topology-aware CPU placement orchestration, kubelet config generation, workload validation | Control plane / operator namespace |
| Node topology agent | Discover and report CPU / NUMA / memory topology to CPU Operator | Worker node DaemonSet |
| CPU Management Policy | Pin CPUs for eligible Guaranteed containers / enforce static CPU allocation | Inside kubelet on worker node |
| Topology Manager | Align CPU and device resources with NUMA topology | Inside kubelet on worker node |
| DRA optional | Claim-based resource allocation and scheduler-visible placement hints | Control-plane APIs plus optional driver on worker node |
| Linux cgroups / cpuset | Final CPU enforcement | Worker node Linux kernel |

## Cleanup

```bash
./scripts/cleanup.sh
```
