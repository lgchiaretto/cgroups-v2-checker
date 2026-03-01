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

Opcionalmente, a aplicacao utiliza `skopeo inspect docker://IMAGE` para ler os metadados remotos da imagem (manifest e config JSON), sem baixar nenhuma camada. Com isso verifica:

- Labels OCI (`org.opencontainers.image.base.name`, `com.redhat.component`)
- Variaveis de ambiente (`JAVA_VERSION`, flags JVM)
- Referencias a paths cgroups v1 no CMD/Entrypoint
- Arquivos cgroups v1 referenciados (ex: `memory.limit_in_bytes`)

### Classificacao de Severidade

| Severidade | Descricao |
|---|---|
| CRITICAL | Imagem base incompativel ou EOL (RHEL 6/7, CentOS 7) |
| HIGH | Runtime com problemas serios de cgroups v2 (Java < 11, Elasticsearch 6.x) |
| MEDIUM | Possivel problema, precisa validacao (Alpine < 3.14, .NET < 6) |
| LOW | Risco baixo, recomendacao de atualizacao |
| OK | Sem problemas detectados |

## Arquitetura

```
Browser  -->  Flask (PatternFly v6 dark theme)
                |
                +--> Kubernetes API (list pods, get cluster info)
                +--> skopeo inspect docker://IMAGE (metadata only, no pull)
                +--> JSON reports saved in /app/data/reports/
```

A aplicacao roda dentro do cluster OpenShift com uma ServiceAccount que tem permissao para listar pods e ler informacoes do cluster.

## Estrutura de Arquivos

```
app/
  __init__.py        # Modulo Python
  app.py             # Flask application factory
  config.py          # Configuracao via variaveis de ambiente
  scanner.py         # Motor de varredura (skopeo + Kubernetes API)
  routes.py          # Rotas web (dashboard, relatorios)
  api.py             # API REST (trigger scan, listar relatorios)
  templates/         # Templates Jinja2 (PatternFly v6 dark theme)
    base.html
    dashboard.html
    report.html
    error.html
  static/
    css/app.css
    js/app.js
openshift/           # Manifests de deploy para OpenShift
  namespace.yaml
  rbac.yaml
  deployment.yaml
Containerfile        # Build da imagem (UBI 9 + skopeo + Python)
gunicorn.conf.py     # Configuracao do Gunicorn
run.py               # Entrypoint da aplicacao
requirements.txt     # Dependencias Python
```

## Deploy no OpenShift

### 1. Build da imagem

```bash
podman build -t quay.io/chiaretto/cgroups-v2-checker:latest -f Containerfile .
podman push quay.io/chiaretto/cgroups-v2-checker:latest
```

### 2. Aplicar os manifests

```bash
oc apply -f openshift/namespace.yaml
oc apply -f openshift/rbac.yaml
oc apply -f openshift/deployment.yaml
```

### 3. Acessar a aplicacao

```bash
oc get route -n cgroups-v2-checker
```

Abra a URL da Route no navegador. A interface web permite:
- Iniciar varreduras sob demanda
- Configurar namespaces especificos ou excluir namespaces
- Habilitar/desabilitar inspecao via skopeo (Nivel 2)
- Visualizar e filtrar resultados por severidade
- Baixar relatorios em JSON

## Variaveis de Ambiente

| Variavel | Padrao | Descricao |
|---|---|---|
| `SECRET_KEY` | (gerado automaticamente) | Chave secreta do Flask |
| `REPORT_DIR` | `/app/data/reports` | Diretorio para salvar relatorios |
| `SKIP_SYSTEM_NAMESPACES` | `true` | Pular namespaces de sistema do OpenShift |
| `SKOPEO_TLS_VERIFY` | `true` | Verificar TLS nas chamadas do skopeo |
| `SKOPEO_AUTH_FILE` | (vazio) | Arquivo de autenticacao para registries privados |
| `SKOPEO_MAX_WORKERS` | `10` | Threads paralelas para inspecao via skopeo |

## API REST

| Metodo | Endpoint | Descricao |
|---|---|---|
| `POST` | `/api/scan` | Iniciar nova varredura |
| `GET` | `/api/scan/<id>` | Status e progresso da varredura |
| `GET` | `/api/reports` | Listar todos os relatorios |
| `GET` | `/api/reports/<id>` | Obter relatorio especifico |
| `DELETE` | `/api/reports/<id>` | Remover relatorio |

### Exemplo: Iniciar varredura

```bash
curl -X POST http://cgroups-v2-checker-route/api/scan \
  -H "Content-Type: application/json" \
  -d '{"namespaces": ["my-app"], "inspect_images": true}'
```

## Desenvolvimento Local

```bash
pip install -r requirements.txt
export REPORT_DIR=./data/reports
python run.py
```

Acesse `http://localhost:8080`. A conexao ao cluster usa o kubeconfig local (`~/.kube/config` ou `$KUBECONFIG`).
