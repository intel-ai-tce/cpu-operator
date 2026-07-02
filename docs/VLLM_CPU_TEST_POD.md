# vLLM CPU Test Pod Generator

This helper generates a vLLM CPU test pod whose CPU request and limit match the current `cpuPodCPUSet` computed by the `CPUPlacementPolicy` operator.

A static pod manifest can easily drift from the current policy. This generator reads the operator output ConfigMap, counts the CPUs in `cpuPodCPUSet`, and writes a Guaranteed-QoS pod manifest with matching `requests.cpu` and `limits.cpu`.

## Generate the test pod

```bash
./scripts/generate-vllm-cpu-test-pod.sh
```

By default, the script reads:

```text
Namespace: cpu-operator-system
Policy:    auto-vllm-cpu-policy
ConfigMap: auto-vllm-cpu-policy-computed-cpu-policy
```

It writes:

```text
examples/vllm-cpu-test-pod.generated.yaml
```

## Apply the generated pod

```bash
oc apply -f examples/vllm-cpu-test-pod.generated.yaml
```

Check the pod:

```bash
oc get pod vllm-cpu-test -n default -o wide
oc logs vllm-cpu-test -n default
```

The log prints both the expected policy CPU set and the actual `Cpus_allowed_list` seen inside the container.

## Example with current policy

If the computed policy contains:

```yaml
cpuPodCPUSet: 3-85,89-171,175-257,261-343
```

then the generated pod will request and limit:

```yaml
resources:
  requests:
    cpu: "332"
  limits:
    cpu: "332"
```

because the CPU set contains 332 logical CPUs.

## Useful overrides

Generate for a specific node:

```bash
NODE=luis.fm2aihpcsed.com ./scripts/generate-vllm-cpu-test-pod.sh
```

Change output path:

```bash
OUT=/tmp/vllm-cpu-test.yaml ./scripts/generate-vllm-cpu-test-pod.sh
```

Change pod namespace, pod name, image, or memory:

```bash
POD_NAMESPACE=default \
POD_NAME=vllm-cpu-test \
IMAGE=registry.access.redhat.com/ubi9/ubi \
MEMORY=256Gi \
./scripts/generate-vllm-cpu-test-pod.sh
```

Use a different policy:

```bash
POLICY=my-cpu-policy ./scripts/generate-vllm-cpu-test-pod.sh
```

Or explicitly set the ConfigMap:

```bash
CM_NAME=my-cpu-policy-computed-cpu-policy ./scripts/generate-vllm-cpu-test-pod.sh
```

## Important limitation

This script makes the pod request the same **number of CPUs** as the computed `cpuPodCPUSet`.

Kubelet CPU Manager can allocate exclusive CPUs for a Guaranteed pod when `cpuManagerPolicy: static` is active. However, kubelet does not understand the operator's conceptual `cpuPodCPUSet` and `gpuPodCPUSet` pools by itself.

Therefore, the actual `Cpus_allowed_list` may not exactly match the recommended `cpuPodCPUSet` unless additional cpuset enforcement is used.
