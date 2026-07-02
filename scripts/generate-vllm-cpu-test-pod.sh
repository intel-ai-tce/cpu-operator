#!/usr/bin/env bash
set -euo pipefail

# Generate a vLLM CPU test pod whose CPU request/limit is derived from the
# current computed CPUPlacementPolicy output ConfigMap.
#
# Required tools: oc, python3
# Optional environment overrides:
#   NAMESPACE       Namespace containing the CPUPlacementPolicy output ConfigMap
#   POLICY          CPUPlacementPolicy name
#   CM_NAME         Computed policy ConfigMap name
#   NODE            Target node name. If omitted, the first node in cpuPlacementByNode.yaml is used.
#   POD_NAMESPACE   Namespace for the generated test pod
#   POD_NAME        Generated pod name
#   IMAGE           Test container image
#   MEMORY          Memory request/limit for the test pod
#   OUT             Output manifest path

NAMESPACE="${NAMESPACE:-cpu-operator-system}"
POLICY="${POLICY:-auto-vllm-cpu-policy}"
CM_NAME="${CM_NAME:-${POLICY}-computed-cpu-policy}"
NODE="${NODE:-}"
POD_NAMESPACE="${POD_NAMESPACE:-default}"
POD_NAME="${POD_NAME:-vllm-cpu-test}"
IMAGE="${IMAGE:-registry.access.redhat.com/ubi9/ubi}"
MEMORY="${MEMORY:-256Gi}"
OUT="${OUT:-examples/vllm-cpu-test-pod.generated.yaml}"

fail() { echo "[FAIL] $*" >&2; exit 1; }
info() { echo "[INFO] $*"; }

command -v oc >/dev/null || fail "oc command not found"
command -v python3 >/dev/null || fail "python3 command not found"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

CPU_PLACEMENT_FILE="${WORKDIR}/cpuPlacementByNode.yaml"
NODE_LABELS_FILE="${WORKDIR}/generatedNodeLabels.yaml"

info "Reading computed placement from ConfigMap ${NAMESPACE}/${CM_NAME}"
oc get cm "${CM_NAME}" -n "${NAMESPACE}" \
  -o jsonpath='{.data.cpuPlacementByNode\.yaml}' > "${CPU_PLACEMENT_FILE}"

# generatedNodeLabels.yaml is useful but optional for older ConfigMaps.
oc get cm "${CM_NAME}" -n "${NAMESPACE}" \
  -o jsonpath='{.data.generatedNodeLabels\.yaml}' > "${NODE_LABELS_FILE}" 2>/dev/null || true

if [[ ! -s "${CPU_PLACEMENT_FILE}" ]]; then
  fail "ConfigMap ${NAMESPACE}/${CM_NAME} does not contain data.cpuPlacementByNode.yaml"
fi

PY_OUT="$(python3 - "${CPU_PLACEMENT_FILE}" "${NODE_LABELS_FILE}" "${NODE}" <<'PY'
import re
import sys
from pathlib import Path

placement_path = Path(sys.argv[1])
labels_path = Path(sys.argv[2])
requested_node = sys.argv[3].strip()

placement_text = placement_path.read_text(encoding="utf-8")
labels_text = labels_path.read_text(encoding="utf-8") if labels_path.exists() else ""


def top_level_keys(text):
    keys = []
    for line in text.splitlines():
        if not line.strip() or line.startswith(" ") or line.startswith("\t"):
            continue
        if line.rstrip().endswith(":"):
            keys.append(line.rstrip()[:-1].strip().strip('"\''))
    return keys


def extract_scalar(text, node, key):
    in_node = False
    pattern = re.compile(rf"^\s{{2}}{re.escape(key)}:\s*(.*)$")
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.rstrip().endswith(":"):
            current = line.rstrip()[:-1].strip().strip('"\'')
            in_node = current == node
            continue
        if in_node:
            match = pattern.match(line)
            if match:
                return match.group(1).strip().strip('"\'')
    return ""


def count_cpuset(cpuset):
    total = 0
    for part in cpuset.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            start_s, end_s = part.split('-', 1)
            start = int(start_s)
            end = int(end_s)
            if end < start:
                raise ValueError(f"Invalid CPU range: {part}")
            total += end - start + 1
        else:
            int(part)
            total += 1
    return total

nodes = top_level_keys(placement_text)
if not nodes:
    raise SystemExit("No nodes found in cpuPlacementByNode.yaml")

node = requested_node or nodes[0]
if node not in nodes:
    raise SystemExit(f"Requested NODE={node!r} was not found. Available nodes: {', '.join(nodes)}")

cpu_set = extract_scalar(placement_text, node, "cpuPodCPUSet")
if not cpu_set:
    raise SystemExit(f"Node {node!r} does not have cpuPodCPUSet in cpuPlacementByNode.yaml")

cpu_count = count_cpuset(cpu_set)
node_class = extract_scalar(labels_text, node, "cpu.example.com/node-class") or extract_scalar(placement_text, node, "nodeClass")
topology_group = extract_scalar(labels_text, node, "cpu.example.com/topology-group")
placement_strategy = extract_scalar(labels_text, node, "cpu.example.com/placement-strategy")
phase4_applied = extract_scalar(labels_text, node, "cpu.example.com/phase4-applied")

print(f"NODE={node}")
print(f"CPUSET={cpu_set}")
print(f"CPU_COUNT={cpu_count}")
print(f"NODE_CLASS={node_class}")
print(f"TOPOLOGY_GROUP={topology_group}")
print(f"PLACEMENT_STRATEGY={placement_strategy}")
print(f"PHASE4_APPLIED={phase4_applied}")
PY
)"

# shellcheck disable=SC1090
source <(printf '%s\n' "${PY_OUT}")

if [[ -z "${CPUSET:-}" || -z "${CPU_COUNT:-}" ]]; then
  fail "Failed to derive CPUSET/CPU_COUNT from ${CM_NAME}"
fi

mkdir -p "$(dirname "${OUT}")"

cat > "${OUT}" <<EOF_MANIFEST
apiVersion: v1
kind: Pod
metadata:
  name: ${POD_NAME}
  namespace: ${POD_NAMESPACE}
  labels:
    app: vllm-cpu-test
    workload: vllm-cpu
    cpu.example.com/generated-from-policy: ${POLICY}
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${NODE}
    cpu.example.com/placement-ready: "true"
EOF_MANIFEST

if [[ -n "${NODE_CLASS:-}" ]]; then
  cat >> "${OUT}" <<EOF_MANIFEST
    cpu.example.com/node-class: ${NODE_CLASS}
EOF_MANIFEST
fi

if [[ -n "${TOPOLOGY_GROUP:-}" ]]; then
  cat >> "${OUT}" <<EOF_MANIFEST
    cpu.example.com/topology-group: ${TOPOLOGY_GROUP}
EOF_MANIFEST
fi

if [[ -n "${PHASE4_APPLIED:-}" ]]; then
  cat >> "${OUT}" <<EOF_MANIFEST
    cpu.example.com/phase4-applied: "${PHASE4_APPLIED}"
EOF_MANIFEST
fi

cat >> "${OUT}" <<EOF_MANIFEST
  containers:
    - name: vllm-cpu-test
      image: ${IMAGE}
      imagePullPolicy: IfNotPresent
      command:
        - /bin/bash
        - -lc
        - |
          echo "=== vLLM CPU placement test pod ==="
          echo "Node: \${NODE_NAME}"
          echo "Policy: ${POLICY}"
          echo "Expected policy CPU set: ${CPUSET}"
          echo "Expected CPU request/limit: ${CPU_COUNT}"
          echo "Expected node class: ${NODE_CLASS:-unknown}"
          echo "Expected topology group: ${TOPOLOGY_GROUP:-unknown}"
          echo "Expected placement strategy: ${PLACEMENT_STRATEGY:-unknown}"
          echo
          echo "Actual Cpus_allowed_list from /proc/self/status:"
          grep Cpus_allowed_list /proc/self/status
          echo
          echo "Container will sleep for inspection."
          sleep infinity
      env:
        - name: NODE_NAME
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        - name: VLLM_CPU_KVCACHE_SPACE
          value: "40"
        - name: VLLM_CPU_SGL_KERNEL
          value: "1"
        - name: VLLM_RPC_TIMEOUT
          value: "100000"
        - name: VLLM_ENGINE_ITERATION_TIMEOUT_S
          value: "120"
      resources:
        requests:
          cpu: "${CPU_COUNT}"
          memory: "${MEMORY}"
        limits:
          cpu: "${CPU_COUNT}"
          memory: "${MEMORY}"
EOF_MANIFEST

info "Generated ${OUT}"
echo "NODE=${NODE}"
echo "CPUSET=${CPUSET}"
echo "CPU_COUNT=${CPU_COUNT}"
echo "NODE_CLASS=${NODE_CLASS:-}"
echo "TOPOLOGY_GROUP=${TOPOLOGY_GROUP:-}"
echo
echo "Apply with:"
echo "  oc apply -f ${OUT}"
echo
echo "Validate with:"
echo "  oc get pod ${POD_NAME} -n ${POD_NAMESPACE} -o wide"
echo "  oc logs ${POD_NAME} -n ${POD_NAMESPACE}"
