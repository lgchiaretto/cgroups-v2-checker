# cgroups v2 Checker - Documentacao

Aplicacao web para varrer clusters OpenShift e identificar imagens de container com problemas de compatibilidade com cgroups v2 antes do upgrade para o OpenShift 4.19.

## Contexto

O OpenShift 4.19 utiliza RHCOS 9, que opera exclusivamente com cgroups v2 (unified hierarchy). Imagens baseadas em sistemas operacionais antigos (RHEL 7, CentOS 7, Ubuntu < 20.04, etc.) ou com runtimes desatualizados (JDK < 15, .NET < 6) podem apresentar falhas criticas no novo ambiente.

## Como Funciona

### Nivel 1 - Analise por nome/tag

A aplicacao lista todos os pods do cluster via API do Kubernetes e analisa os nomes das imagens usando regras de deteccao:

- **Imagens base problematicas**: RHEL/CentOS 6/7, Debian < 11, Ubuntu < 20.04, Alpine < 3.14, Amazon Linux 1/2, Oracle Linux 7, SLES 12
- **Runtimes desatualizados**: Java (< JDK 15 sem patch), .NET (< 6), Node.js (< 16), Python 2, Go (< 1.19)
- **Softwares conhecidos**: MySQL 5.x, PostgreSQL < 14, Elasticsearch < 8.x, Kafka < 3.x, Jenkins com JDK8, WildFly < 26, Tomcat < 10

### Nivel 2 - Inspecao remota via skopeo

Utiliza `skopeo inspect docker://IMAGE` para ler os metadados remotos da imagem (manifest e config JSON), sem baixar nenhuma camada:

- Labels OCI (`org.opencontainers.image.base.name`, `com.redhat.component`)
- Variaveis de ambiente (`JAVA_VERSION`, flags JVM)
- Referencias a paths cgroups v1 no CMD/Entrypoint
- Arquivos cgroups v1 referenciados (ex: `memory.limit_in_bytes`)
- Deteccao de middleware Red Hat (JBoss EAP, Data Grid via labels)

Imagens sem labels de imagem base ou identificacao de OS sao sinalizadas com "Insufficient Metadata" (severidade LOW), recomendando exec check ou adicao de labels OCI. O relatorio JSON inclui um bloco `inspection_metadata` para cada imagem inspecionada mostrando o que o skopeo encontrou (quantidade de labels, labels relevantes, variaveis de ambiente), fornecendo uma trilha de auditoria clara para o status OK.

### Nivel 3 - Deteccao em Runtime via pod exec (opcional)

Executa um script de deteccao dentro dos pods em execucao para verificar uso de cgroups v1:

- Se o pod roda sob hierarquia cgroups v1 ou v2
- Arquivos da aplicacao que referenciam paths v1
- Variaveis de ambiente referenciando cgroups v1

### Classificacao de Severidade

| Severidade | Descricao |
|---|---|
| CRITICAL | Imagem base incompativel ou EOL (RHEL 6/7, CentOS 7) |
| HIGH | Runtime com problemas serios de cgroups v2 (Java < 11, Elasticsearch 6.x) |
| MEDIUM | Possivel problema, precisa validacao (Alpine < 3.14, .NET < 6) |
| LOW | Risco baixo ou metadados insuficientes para validacao |
| INFO | Finding informativo |
| OK | Sem problemas detectados (metadados validados) |
| UNKNOWN | Nao inspecionado (skopeo desabilitado ou falhou) |

Imagens encontradas apenas em initContainers tem a severidade reduzida em um nivel.

## Arquitetura

```
Browser  -->  Flask (PatternFly v6 dark theme)
                |
                +--> Kubernetes API (list pods, get cluster info)
                +--> skopeo inspect docker://IMAGE (metadata only, no pull)
                +--> pod exec (deteccao runtime opcional)
                +--> JSON reports salvos em /app/data/reports/
```

A aplicacao roda dentro do cluster OpenShift com uma ServiceAccount que tem permissao para listar pods, executar comandos em pods, ler secrets (ImagePullSecrets) e ler informacoes do cluster.

## Estrutura de Arquivos

```
app/
  app.py             Flask application factory
  config.py          Configuracao via variaveis de ambiente
  scanner.py         Motor de varredura (Kubernetes API + skopeo + exec)
  routes.py          Rotas web (dashboard, relatorios, CSV)
  api.py             API REST (/api/scan, /api/reports, /api/registries)
  templates/         Templates Jinja2 (PatternFly v6 dark theme)
    base.html        Layout base com navegacao lateral
    dashboard.html   Pagina inicial com controles de scan
    report.html      Visualizacao de relatorio com cards, filtros, paginacao
    registries.html  Gerenciamento de credenciais de registry
    error.html       Pagina de erro
  static/
    css/app.css      Estilos customizados (dark theme, paginacao, cards)
    js/app.js        Interacao do dashboard
    icons/           Icones SVG
openshift/           Manifests de deploy para OpenShift
  namespace.yaml     Definicao do Namespace
  rbac.yaml          ServiceAccount, ClusterRole, ClusterRoleBinding
  deployment.yaml    Deployment, Service, Route
setup.sh             Script de build, push, deploy e gerenciamento
Containerfile        Build da imagem (UBI 9 + skopeo + Python 3.11)
gunicorn.conf.py     Configuracao do Gunicorn (1 worker, 4 threads)
run.py               Entrypoint da aplicacao
requirements.txt     Dependencias Python
```

## Deploy no OpenShift

### Deploy padrao

```bash
# Pipeline completa: build, push para Quay.io, deploy, restart
./setup.sh --build-push-deploy

# Passos individuais
./setup.sh --build      # Build da imagem
./setup.sh --push       # Push para Quay.io
./setup.sh --deploy     # Aplicar manifests no OpenShift
./setup.sh --restart    # Rollout restart

# Remover do cluster
./setup.sh --remove
```

### Deploy com proxy corporativo

Quando o cluster esta atras de um proxy corporativo que intercepta TLS, o pull de imagens de registries externos pode falhar. Use `--mirror` para enviar a imagem diretamente para o registro interno do OCP, sem passar pelo proxy:

```bash
# Build local e push para o registro interno (recomendado para ambientes com proxy)
./setup.sh --mirror --build-push-deploy

# Auto-detectar proxy do cluster e injetar no deployment (para o skopeo funcionar)
./setup.sh --mirror --proxy auto --build-push-deploy

# Usar URL de proxy especifica
./setup.sh --mirror --proxy http://proxy.corp.com:8080 --build-push-deploy
```

### Referencia do setup.sh

```
Uso: ./setup.sh [opcoes globais] <acao>

Opcoes Globais:
  --proxy <URL|auto>       Configura proxy HTTP/HTTPS (usado no build, push,
                           e injetado no Deployment do OpenShift).
                           Use "auto" para ler do objeto Proxy do cluster.
  --no-proxy <hosts>       Hosts NO_PROXY separados por virgula (opcional,
                           auto-detectado do cluster ou usa defaults)
  --mirror                 Envia imagem para o registro interno do OCP
                           ao inves do Quay.io (evita problemas de proxy/TLS)

Acoes:
  Desenvolvimento Local:
    --build                Build da imagem
    --run-local            Rodar localmente com Podman
    --stop                 Parar container local
    --status               Status do container local
    --logs                 Logs do container local
    --destroy              Remover container, imagem e dados

  Deploy no OpenShift:
    --push                 Push para Quay.io
    --deploy               Deploy no OpenShift (aplicar manifests)
    --build-push-deploy    Pipeline completa: build, push, deploy, restart
    --restart              Restart do deployment
    --persistent           Persistir credenciais de registry em K8s Secret
    --openshift-status     Status dos recursos no OpenShift
    --openshift-logs       Logs do deployment
    --remove               Remover app completamente do OpenShift
```

**Como funciona o `--mirror`:**

1. Expoe a rota externa do registro interno do OCP (se nao existir)
2. Faz login usando o token da sessao `oc` atual
3. Tag e push da imagem para `image-registry.openshift-image-registry.svc:5000/<namespace>/<app>`
4. Atualiza o Deployment para usar a referencia da imagem interna

**Como funciona o `--proxy`:**

| Acao | Efeito |
|---|---|
| `--build` | Passa `http_proxy`/`https_proxy` como `--build-arg` para o podman |
| `--push` | Exporta variaveis de proxy para o podman push |
| `--deploy` | Injeta `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` no Deployment via `oc set env` |
| `auto` | Le configuracao de proxy de `oc get proxy cluster` (httpProxy, httpsProxy, noProxy) |

## Desenvolvimento Local

```bash
pip install -r requirements.txt
export REPORT_DIR=./data/reports
python run.py
```

Acesse `http://localhost:8080`. A conexao ao cluster usa o kubeconfig local (`~/.kube/config` ou `$KUBECONFIG`).

## Variaveis de Ambiente

| Variavel | Padrao | Descricao |
|---|---|---|
| `SECRET_KEY` | (gerado automaticamente) | Chave secreta do Flask |
| `REPORT_DIR` | `/app/data/reports` | Diretorio para salvar relatorios |
| `SKIP_SYSTEM_NAMESPACES` | `true` | Pular namespaces de sistema do OpenShift |
| `SKOPEO_TLS_VERIFY` | `true` | Verificar TLS nas chamadas do skopeo |
| `SKOPEO_AUTH_FILE` | (vazio) | Arquivo de autenticacao para registries privados |
| `SKOPEO_MAX_WORKERS` | `20` | Threads paralelas para inspecao via skopeo |
| `USE_IMAGE_PULL_SECRETS` | `true` | Extrair auth de registries dos pods/ServiceAccounts |
| `REGISTRIES_FILE` | (vazio) | Caminho para registries.json montado de K8s Secret |

## API REST

| Metodo | Endpoint | Descricao |
|---|---|---|
| `POST` | `/api/scan` | Iniciar nova varredura |
| `GET` | `/api/scan/<id>` | Status e progresso da varredura |
| `GET` | `/api/reports` | Listar todos os relatorios |
| `GET` | `/api/reports/<id>` | Obter relatorio especifico |
| `DELETE` | `/api/reports/<id>` | Remover relatorio |
| `GET` | `/api/registries` | Listar credenciais de registry |
| `POST` | `/api/registries` | Adicionar credenciais de registry |
| `DELETE` | `/api/registries/<host>` | Remover credenciais de registry |

### Exemplo: Iniciar varredura

```bash
curl -X POST http://cgroups-v2-checker-route/api/scan \
  -H "Content-Type: application/json" \
  -d '{"namespaces": ["my-app"], "inspect_images": true, "exec_check": true}'
```

### Opcoes de varredura

| Campo | Tipo | Padrao | Descricao |
|---|---|---|---|
| `namespaces` | list | todos | Namespaces especificos para varrer |
| `exclude_namespaces` | list | nenhum | Namespaces para excluir |
| `namespace_patterns` | list | nenhum | Padroes regex para incluir namespaces |
| `exclude_patterns` | list | nenhum | Padroes regex para excluir namespaces |
| `inspect_images` | bool | true | Habilitar inspecao via skopeo (Nivel 2) |
| `exec_check` | bool | false | Habilitar deteccao runtime via pod exec (Nivel 3) |
