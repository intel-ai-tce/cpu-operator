# CPU Operator Demo

Minimal OpenShift/Kubernetes CPU Operator proof-of-concept.

This demo avoids NRI and treats DRA as optional. It does **not** modify real kubelet settings yet. It discovers worker-node CPU/NUMA topology, computes a recommended `reservedSystemCPUs` value, and stores the result in a ConfigMap.

## Components

```text
Node topology agent DaemonSet
  -> runs on every worker node
  -> reads /sys CPU and NUMA topology
  -> writes NodeCPUTopology.status

CPU Operator Deployment
  -> watches NodeCPUTopology and CPUPlacementPolicy
  -> computes NUMA-balanced reservedSystemCPUs
  -> writes <policy-name>-computed-cpu-policy ConfigMap
```

## Files

```text
crds.yaml
rbac.yaml
agent/daemonset.yaml
operator/deployment.yaml
examples/cpu-placement-policy.yaml
scripts/sanity-check.sh
docs/README_internal_registry.md
docs/README_external_registry.md
```

## Required fixes

Validate the bad Kopf logging setting is absent:

```bash
grep -n 'settings.posting.level = "INFO"' operator/operator.py || echo "OK"
```

Validate RBAC after applying `rbac.yaml`:

```bash
oc auth can-i list namespaces \
  --as system:serviceaccount:cpu-operator-system:cpu-operator

oc auth can-i list customresourcedefinitions.apiextensions.k8s.io \
  --as system:serviceaccount:cpu-operator-system:cpu-operator
```

Expected:

```text
yes
yes
```

## Worker-node scheduling

The topology agent runs on all worker nodes:

```yaml
nodeSelector:
  node-role.kubernetes.io/worker: ""
```

Check workers:

```bash
oc get nodes -l node-role.kubernetes.io/worker=
```

## Registry setup

Choose one registry guide:

```text
docs/README_internal_registry.md
docs/README_external_registry.md
```

## Deploy

After image names are updated in `agent/daemonset.yaml` and `operator/deployment.yaml`:

```bash
oc apply -f crds.yaml
oc apply -f rbac.yaml

oc adm policy add-scc-to-user privileged \
  -z node-topology-agent \
  -n cpu-operator-system

oc apply -f agent/daemonset.yaml
oc apply -f operator/deployment.yaml
oc apply -f examples/cpu-placement-policy.yaml
```

## CPUPlacementPolicy

`examples/cpu-placement-policy.yaml` is the input policy that triggers computation:

```yaml
apiVersion: cpu.example.com/v1alpha1
kind: CPUPlacementPolicy
metadata:
  name: vllm-cpu-policy
  namespace: cpu-operator-system
spec:
  cpuManagerPolicy: static
  topologyManagerPolicy: restricted
  systemReserved:
    logicalCPUsPerNuma: 40
  nodeSelector:
    node-role.kubernetes.io/worker: ""
```

The output ConfigMap is:

```text
vllm-cpu-policy-computed-cpu-policy
```

View it:

```bash
oc get cm vllm-cpu-policy-computed-cpu-policy \
  -n cpu-operator-system \
  -o yaml
```

## Sanity test

Run:

```bash
./scripts/sanity-check.sh
```

Optional variables:

```bash
NAMESPACE=cpu-operator-system POLICY=vllm-cpu-policy ./scripts/sanity-check.sh
```

The script checks:

```text
Pods are Running
node-topology-agent count vs worker-node count
CPU Operator RBAC
NodeCPUTopology readiness and CPU/NUMA fields
CPUPlacementPolicy existence
computed ConfigMap existence
computed reservedSystemCPUs matches NodeCPUTopology + CPUPlacementPolicy
oc logs availability, as a warning only
```

## Important limitation

The generated ConfigMap does not change worker nodes directly. It is a computed recommendation.

To affect worker nodes in production, a later operator version would create or patch OpenShift `KubeletConfig` / `PerformanceProfile`, causing MachineConfig rollout and kubelet CPU Manager/Topology Manager configuration.

## Cleanup

```bash
./scripts/cleanup.sh
```
