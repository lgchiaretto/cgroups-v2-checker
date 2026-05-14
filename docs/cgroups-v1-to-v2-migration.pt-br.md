# Migração de cgroups v1 para cgroups v2 no OpenShift

Este procedimento descreve como migrar um cluster OpenShift de cgroups v1 para cgroups v2. A partir do OCP 4.19, cgroups v2 é o padrão e o suporte a cgroups v1 está depreciado.

> **Atenção:** Este procedimento causará um reboot rolling de **todos os nodes** do cluster. Planeje uma janela de manutenção adequada.

## Pré-requisitos

- Acesso de cluster admin (CLI `oc` autenticado com role `cluster-admin`)
- Todos os nodes em estado `Ready`
- Nenhuma atualização de cluster ou rollout de MachineConfig em andamento
- (Recomendado) Executar o **cgroups v2 compatibility checker** previamente para identificar workloads que possam quebrar

## Procedimento

### Passo 1 — Verificar o modo atual de cgroup

Verifique a configuração atual do cgroup:

```bash
oc get nodes.config/cluster -o yaml
```

Saída esperada (antes da migração):

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

Se `spec.cgroupMode` não estiver definido ou estiver vazio, o cluster está rodando cgroups v1 (o padrão anterior).

### Passo 2 — (Opcional) Pausar os MachineConfigPools de worker

> Este passo é **opcional**. Se preferir deixar o MCO gerenciar o reboot rolling automaticamente, pule direto para o [Passo 3](#passo-3--definir-cgroupmode-para-v2).

Pausar os MCPs de worker evita que os nodes reiniciem imediatamente quando a configuração mudar, dando controle sobre a ordem e o timing do rollout.

> **Importante:** Nunca pause o MCP `master`. O control plane deve sempre poder aplicar mudanças de configuração livremente.

Liste todos os MCPs:

```bash
oc get machineconfigpools
```

Exemplo de saída:

```
NAME                          CONFIG                        UPDATED   UPDATING   DEGRADED   MACHINECOUNT   READYMACHINECOUNT
master                        rendered-master-a1b2c3d4e5    True      False      False      3              3
worker                        rendered-worker-f6g7h8i9j0    True      False      False      12             12
worker-app                    rendered-worker-app-k1l2m3    True      False      False      23             23
worker-middleware             rendered-worker-mw-n4o5p6     True      False      False      1              1
worker-infra                  rendered-worker-infra-q7r8s9  True      False      False      2              2
```

Pause cada MCP de **worker**:

```bash
oc patch mcp worker --type='merge' --patch '{"spec":{"paused":true}}'
oc patch mcp worker-app --type='merge' --patch '{"spec":{"paused":true}}'
oc patch mcp worker-middleware --type='merge' --patch '{"spec":{"paused":true}}'
oc patch mcp worker-infra --type='merge' --patch '{"spec":{"paused":true}}'
```

Verifique se os MCPs de worker estão pausados:

```bash
oc get mcp -o custom-columns=NAME:.metadata.name,PAUSED:.spec.paused
```

Saída esperada:

```
NAME                   PAUSED
master                 <none>
worker                 true
worker-app             true
worker-middleware      true
worker-infra           true
```

### Passo 3 — Definir cgroupMode para v2

Edite a configuração de nodes para habilitar cgroups v2:

```bash
oc edit nodes.config/cluster
```

Adicione ou modifique `spec.cgroupMode` para `v2`:

```yaml
apiVersion: config.openshift.io/v1
kind: Node
metadata:
  name: cluster
spec:
  cgroupMode: v2
```

Alternativamente, use um comando de patch:

```bash
oc patch nodes.config/cluster --type='merge' --patch '{"spec":{"cgroupMode":"v2"}}'
```

### Passo 4 — Verificar se os novos MachineConfigs foram gerados

Após alterar o cgroupMode, o Machine Config Operator (MCO) irá gerar novos recursos MachineConfig renderizados.

```bash
oc get mcp
```

- Se você **pausou** os MCPs de worker no Passo 2, os pools de worker devem mostrar `UPDATED=False` e `UPDATING=False` (os nodes ainda não reiniciaram), enquanto o pool master já começará a atualizar:

```
NAME                          CONFIG                              UPDATED   UPDATING   DEGRADED   MACHINECOUNT   READYMACHINECOUNT
master                        rendered-master-x9y8z7w6v5          False     True       False      3              3
worker                        rendered-worker-u4t3s2r1q0          False     False      False      12             12
worker-app                    rendered-worker-app-p9o8n7          False     False      False      23             23
worker-middleware             rendered-worker-mw-m6l5k4           False     False      False      1              1
worker-infra                  rendered-worker-infra-j3i2h1        False     False      False      2              2
```

- Se você **não pausou** os MCPs, todos os pools começarão a atualizar (`UPDATING=True`) e os nodes reiniciarão automaticamente. Pule para o [Passo 6](#passo-6--verificar-a-migração) e monitore até que todos os pools mostrem `UPDATED=True`.

### Passo 5 — (Se os MCPs foram pausados) Despausar os MCPs de worker e monitorar o reboot rolling

> Este passo só se aplica se você pausou os MCPs de worker no [Passo 2](#passo-2--opcional-pausar-os-machineconfigpools-de-worker). Se não pausou, pule para o [Passo 6](#passo-6--verificar-a-migração).

Antes de despausar os MCPs de worker, aguarde o pool master finalizar a atualização (como o MCP master nunca foi pausado, ele inicia o rolling automaticamente após o Passo 3):

```bash
watch -n 5 'oc get mcp master'
```

Aguarde até que o MCP master mostre `UPDATED=True`:

```
NAME     CONFIG                         UPDATED   UPDATING   DEGRADED   MACHINECOUNT   READYMACHINECOUNT
master   rendered-master-x9y8z7w6v5     True      False      False      3              3
```

Com o control plane estável, despause os MCPs de worker um de cada vez.

**5a. Despausar os MCPs de worker (um de cada vez):**

```bash
oc patch mcp worker --type='merge' --patch '{"spec":{"paused":false}}'
```

Monitore os worker nodes:

```bash
watch -n 10 'oc get nodes -l node-role.kubernetes.io/worker='
```

Exemplo de saída durante o rollout:

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

Aguarde até o MCP `worker` estar totalmente atualizado, depois continue com os MCPs restantes:

```bash
oc patch mcp worker-app --type='merge' --patch '{"spec":{"paused":false}}'
# Aguarde worker-app completar...

oc patch mcp worker-middleware --type='merge' --patch '{"spec":{"paused":false}}'
# Aguarde worker-middleware completar...

oc patch mcp worker-infra --type='merge' --patch '{"spec":{"paused":false}}'
# Aguarde worker-infra completar...
```

### Passo 6 — Verificar a migração

**6a. Verificar se todos os MCPs estão atualizados:**

```bash
oc get mcp
```

Todos os pools devem mostrar `UPDATED=True`, `UPDATING=False`, `DEGRADED=False`.

**6b. Verificar se todos os nodes estão Ready:**

```bash
oc get nodes
```

Todos os nodes devem estar no status `Ready` sem `SchedulingDisabled`.

**6c. Verificar o modo de cgroup nos nodes:**

Use um debug pod para confirmar:

```bash
oc debug node/ocp-test-x7k9v-master-0 -- chroot /host cat /sys/fs/cgroup/cgroup.controllers
```

Se o comando retornar uma lista de controllers (ex: `cpuset cpu io memory hugetlb pids`), o node está rodando cgroups v2.

No cgroups v1, o arquivo `/sys/fs/cgroup/cgroup.controllers` não existe. Ao invés disso, você veria diretórios individuais de controllers em `/sys/fs/cgroup/`.

**6d. Verificar o status da configuração de nodes:**

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

### Passo 7 — Validação pós-migração

1. Verifique se os workloads críticos estão funcionando corretamente
2. Verifique pods em estado `CrashLoopBackOff` ou `Error`:

   ```bash
   oc get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded | grep -v Completed
   ```

3. Monitore os alertas do cluster para problemas relacionados a cgroups
4. Valide se os resource limits e requests estão sendo aplicados corretamente

## Rollback

Se problemas forem encontrados após a migração, é possível reverter para cgroups v1:

```bash
oc patch nodes.config/cluster --type='merge' --patch '{"spec":{"cgroupMode":"v1"}}'
```

Isso irá iniciar outro reboot rolling de todos os nodes.

> **Nota:** Quando o suporte a cgroups v1 for completamente removido em uma versão futura do OCP, o rollback não será mais possível.

## Referências

- [Documentação OpenShift — Configurando cgroup v2](https://docs.openshift.com/container-platform/latest/post_installation_configuration/machine-configuration-tasks.html#nodes-nodes-cgroups-2_post-install-machine-configuration-tasks)
- [Red Hat KB — Migração de cgroups v2](https://access.redhat.com/solutions/7065961)
