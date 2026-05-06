#!/usr/bin/env bash
set -euo pipefail

oc apply -f crds.yaml
oc apply -f rbac.yaml
oc apply -f agent/daemonset.yaml
oc apply -f operator/deployment.yaml
oc apply -f examples/cpu-placement-policy.yaml

echo
echo "Check status:"
echo "  oc get pods -n cpu-operator-system -o wide"
echo "  oc get nodecputopologies -n cpu-operator-system"
echo "  oc get cm vllm-cpu-policy-computed-cpu-policy -n cpu-operator-system -o yaml"
