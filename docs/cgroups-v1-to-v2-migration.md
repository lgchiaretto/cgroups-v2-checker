# Migrating from cgroups v1 to cgroups v2 on OpenShift

This procedure describes how to migrate an OpenShift cluster from cgroups v1 to cgroups v2. Starting with OCP 4.19, cgroups v2 is the default and cgroups v1 support is deprecated.

> **Warning:** This procedure will cause a rolling reboot of **all nodes** in the cluster. Plan an appropriate maintenance window.

## Prerequisites

- Cluster admin access (`oc` CLI authenticated with `cluster-admin` role)
- All nodes in `Ready` state
- No ongoing cluster upgrades or MachineConfig rollouts
- (Recommended) Run the **cgroups v2 compatibility checker** beforehand to identify workloads that may break

## Procedure

### Step 1 — Verify current cgroup mode

Check the current cgroup configuration:

```bash
oc get nodes.config/cluster -o yaml
```

Expected output (before migration):

```yaml
apiVersion: config.openshift.io/v1
kind: Node
metadata:
  name: cluster
spec: {}
status:
  conditions:
    - lastTransitionTime: "2026-05-10T14:22:00Z"
      message: >-
        cgroups v1 support will soon be deprecated in OpenShift, consider switching
        to cgroups v2
      reason: CGroupModeV1
```

If `spec.cgroupMode` is not set or is empty, the cluster is running cgroups v1 (the previous default).

### Step 2 — (Optional) Pause worker MachineConfigPools

> This step is **optional**. If you prefer to let the MCO handle the rolling reboot automatically, skip directly to [Step 3](#step-3--set-cgroupmode-to-v2).

Pausing worker MCPs prevents nodes from rebooting immediately when the configuration changes, giving you control over the rollout order and timing.

> **Important:** Never pause the `master` MCP. The control plane must always be able to roll out configuration changes freely.

List all MCPs:

```bash
oc get machineconfigpools
```

Example output:

```
NAME                          CONFIG                        UPDATED   UPDATING   DEGRADED   MACHINECOUNT   READYMACHINECOUNT
master                        rendered-master-a1b2c3d4e5    True      False      False      3              3
worker                        rendered-worker-f6g7h8i9j0    True      False      False      12             12
worker-app                    rendered-worker-app-k1l2m3    True      False      False      23             23
worker-middleware             rendered-worker-mw-n4o5p6     True      False      False      1              1
worker-infra                  rendered-worker-infra-q7r8s9  True      False      False      2              2
```

Pause each **worker** MCP:

```bash
oc patch mcp worker --type='merge' --patch '{"spec":{"paused":true}}'
oc patch mcp worker-app --type='merge' --patch '{"spec":{"paused":true}}'
oc patch mcp worker-middleware --type='merge' --patch '{"spec":{"paused":true}}'
oc patch mcp worker-infra --type='merge' --patch '{"spec":{"paused":true}}'
```

Verify the worker MCPs are paused:

```bash
oc get mcp -o custom-columns=NAME:.metadata.name,PAUSED:.spec.paused
```

Expected output:

```
NAME                   PAUSED
master                 <none>
worker                 true
worker-app             true
worker-middleware      true
worker-infra           true
```

### Step 3 — Set cgroupMode to v2

Edit the node configuration to enable cgroups v2:

```bash
oc edit nodes.config/cluster
```

Add or modify `spec.cgroupMode` to `v2`:

```yaml
apiVersion: config.openshift.io/v1
kind: Node
metadata:
  name: cluster
spec:
  cgroupMode: v2
```

Alternatively, use a patch command:

```bash
oc patch nodes.config/cluster --type='merge' --patch '{"spec":{"cgroupMode":"v2"}}'
```

### Step 4 — Verify the new MachineConfigs were generated

After changing the cgroupMode, the Machine Config Operator (MCO) will generate new rendered MachineConfig resources.

```bash
oc get mcp
```

- If you **paused** the worker MCPs in Step 2, you should see `UPDATED=False` and `UPDATING=False` for the worker pools (nodes won't reboot yet), while the master pool will start updating immediately:

```
NAME                          CONFIG                              UPDATED   UPDATING   DEGRADED   MACHINECOUNT   READYMACHINECOUNT
master                        rendered-master-x9y8z7w6v5          False     True       False      3              3
worker                        rendered-worker-u4t3s2r1q0          False     False      False      12             12
worker-app                    rendered-worker-app-p9o8n7          False     False      False      23             23
worker-middleware             rendered-worker-mw-m6l5k4           False     False      False      1              1
worker-infra                  rendered-worker-infra-j3i2h1        False     False      False      2              2
```

- If you **did not pause** the MCPs, all pools will begin updating (`UPDATING=True`) and nodes will start rebooting automatically. Skip to [Step 6](#step-6--verify-the-migration) and monitor until all pools show `UPDATED=True`.

### Step 5 — (If MCPs were paused) Unpause worker MCPs and monitor the rolling reboot

> This step only applies if you paused the worker MCPs in [Step 2](#step-2--optional-pause-worker-machineconfigpools). If you did not pause them, skip to [Step 6](#step-6--verify-the-migration).

Before unpausing worker MCPs, wait for the master pool to finish updating (since the master MCP was never paused, it will start rolling automatically after Step 3):

```bash
watch -n 5 'oc get mcp master'
```

Wait until the master MCP shows `UPDATED=True`:

```
NAME     CONFIG                         UPDATED   UPDATING   DEGRADED   MACHINECOUNT   READYMACHINECOUNT
master   rendered-master-x9y8z7w6v5     True      False      False      3              3
```

Once the control plane is stable, unpause worker MCPs one at a time.

**5a. Unpause worker MCPs (one at a time):**

```bash
oc patch mcp worker --type='merge' --patch '{"spec":{"paused":false}}'
```

Monitor worker nodes:

```bash
watch -n 10 'oc get nodes -l node-role.kubernetes.io/worker='
```

Example output during rollout:

```
NAME                                              STATUS                      ROLES    AGE    VERSION
ocp-test-x7k9v-worker-e4s-v5-pool1-ab1cd         Ready                       worker   287d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool1-ef2gh         Ready,SchedulingDisabled    worker   126d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool1-ij3kl         Ready                       worker   126d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool2-mn4op         Ready                       worker   295d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool2-qr5st         Ready                       worker   34d    v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool2-uv6wx         Ready                       worker   330d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool3-yz7ab         Ready                       worker   310d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool3-cd8ef         Ready                       worker   310d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool3-gh9ij         Ready                       worker   224d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool3-kl0mn         Ready                       worker   100d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool3-op1qr         Ready                       worker   370d   v1.31.14
ocp-test-x7k9v-worker-e4s-v5-pool3-st2uv         Ready                       worker   413d   v1.31.14
```

Wait until the `worker` MCP is fully updated, then continue with the remaining MCPs:

```bash
oc patch mcp worker-app --type='merge' --patch '{"spec":{"paused":false}}'
# Wait for worker-app to complete...

oc patch mcp worker-middleware --type='merge' --patch '{"spec":{"paused":false}}'
# Wait for worker-middleware to complete...

oc patch mcp worker-infra --type='merge' --patch '{"spec":{"paused":false}}'
# Wait for worker-infra to complete...
```

### Step 6 — Verify the migration

**6a. Verify all MCPs are updated:**

```bash
oc get mcp
```

All pools should show `UPDATED=True`, `UPDATING=False`, `DEGRADED=False`.

**6b. Verify all nodes are Ready:**

```bash
oc get nodes
```

All nodes should be in `Ready` status with no `SchedulingDisabled`.

**6c. Verify cgroup mode on the nodes:**

SSH into a node or use debug pod to confirm:

```bash
oc debug node/ocp-test-x7k9v-master-0 -- chroot /host cat /sys/fs/cgroup/cgroup.controllers
```

If the command returns a list of controllers (e.g., `cpuset cpu io memory hugetlb pids`), the node is running cgroups v2.

On cgroups v1, the file `/sys/fs/cgroup/cgroup.controllers` does not exist. Instead you'd see individual controller directories under `/sys/fs/cgroup/`.

**6d. Verify the node config status:**

```bash
oc get nodes.config/cluster -o yaml
```

```yaml
spec:
  cgroupMode: v2
status:
  conditions:
    - lastTransitionTime: "2026-05-14T15:30:00Z"
      message: ""
      reason: CGroupModeV2
```

### Step 7 — Post-migration validation

1. Verify critical workloads are running correctly
2. Check for pods in `CrashLoopBackOff` or `Error` state:

   ```bash
   oc get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded | grep -v Completed
   ```

3. Monitor cluster alerts for any cgroup-related issues
4. Validate resource limits and requests are being enforced correctly

## Rollback

If issues are found after migration, you can revert to cgroups v1:

```bash
oc patch nodes.config/cluster --type='merge' --patch '{"spec":{"cgroupMode":"v1"}}'
```

This will trigger another rolling reboot of all nodes.

> **Note:** Once cgroups v1 support is fully removed in a future OCP release, rollback will no longer be possible.

## References

- [OpenShift Documentation — Configuring cgroup v2](https://docs.openshift.com/container-platform/latest/post_installation_configuration/machine-configuration-tasks.html#nodes-nodes-cgroups-2_post-install-machine-configuration-tasks)
- [Red Hat KB — cgroups v2 migration](https://access.redhat.com/solutions/7065961)
