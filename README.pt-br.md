# cgroups v2 Checker -- Documentacao

Aplicacao web para varrer clusters OpenShift e identificar imagens de container com problemas de compatibilidade com cgroups v2 antes do upgrade para o OpenShift 4.19.


| [English Documentation](README.md) |
| ---------------------------------- |


---

## Contexto

O OpenShift 4.19 utiliza RHCOS 9, que opera exclusivamente com cgroups v2 (unified hierarchy). Imagens baseadas em sistemas operacionais antigos (RHEL 7, CentOS 7, Ubuntu < 20.04, etc.) ou com runtimes desatualizados (JDK < 15, .NET < 6) podem apresentar falhas criticas no novo ambiente.

Esta ferramenta ajuda administradores de cluster a identificar essas imagens antes do upgrade, fornecendo findings classificados por severidade e recomendacoes de correcao.

## Funcionalidades

- Analise de imagens em 3 niveis (regras de nome/tag + metadados remotos via skopeo + deteccao runtime via pod exec)
- Classificacao de severidade: CRITICAL, HIGH, MEDIUM, LOW, INFO, OK, UNKNOWN
- Trilha de auditoria de inspecao (mostra o que o skopeo encontrou para cada imagem)
- Deteccao de metadados insuficientes (sinaliza imagens sem labels/identificacao de OS)
- Cards de severidade clicaveis para filtrar a tabela de resultados
- Modal de drilldown por imagem mostrando pods e namespaces
- Paginacao para listas grandes (20/50/100/Todos)
- Filtro por texto e botoes de filtro por severidade
- Filtro para imagens que falharam na inspecao skopeo
- Download de relatorio em CSV e JSON
- Gerenciamento de credenciais de registry para registries privados
- Detalhamento de pods excluidos (namespaces de sistema vs. excluidos pelo usuario)
- Scan em background com progresso em tempo real
- Tema escuro PatternFly 6 (estilo OpenShift Console)

## Como Funciona

### Nivel 1 -- Analise por nome/tag

Lista todos os pods do cluster via API do Kubernetes e analisa os nomes das imagens usando regras de deteccao:

- **Imagens base problematicas**: RHEL/CentOS 6/7, Debian < 11, Ubuntu < 20.04, Alpine < 3.14, Amazon Linux 1/2, Oracle Linux 7, SLES 12
- **Runtimes desatualizados**: Java (< JDK 15), .NET (< 6), Node.js (< 16), Python 2, Go (< 1.19)
- **Softwares conhecidos**: MySQL 5.x, PostgreSQL < 14, Elasticsearch < 8.x, Kafka < 3.x, Jenkins com JDK8, WildFly < 26, Tomcat < 10

### Nivel 2 -- Inspecao remota via skopeo

Utiliza `skopeo inspect docker://IMAGE` para ler metadados remotos (manifest e config JSON) sem baixar camadas:

- Labels OCI (`org.opencontainers.image.base.name`, `com.redhat.component`)
- Variaveis de ambiente (`JAVA_VERSION`, flags JVM)
- Referencias a paths cgroups v1 no CMD/Entrypoint
- Arquivos cgroups v1 referenciados (`memory.limit_in_bytes`, etc.)
- Deteccao de middleware Red Hat (JBoss EAP, Data Grid via labels)

Imagens sem labels de imagem base ou identificacao de OS sao sinalizadas com "Insufficient Metadata" (severidade LOW), recomendando exec check ou adicao de labels OCI. O relatorio JSON inclui um bloco `inspection_metadata` para cada imagem inspecionada, fornecendo uma trilha de auditoria clara.

### Nivel 3 -- Deteccao em Runtime via pod exec (opcional)

Executa um script de deteccao dentro dos pods em execucao para verificar uso de cgroups v1:

- Se o pod roda sob hierarquia cgroups v1 ou v2
- Arquivos da aplicacao que referenciam paths v1
- Variaveis de ambiente referenciando cgroups v1

### Classificacao de Severidade


| Severidade | Descricao                                                                 |
| ---------- | ------------------------------------------------------------------------- |
| CRITICAL   | Imagem base incompativel ou EOL (RHEL 6/7, CentOS 7)                      |
| HIGH       | Runtime com problemas serios de cgroups v2 (Java < 11, Elasticsearch 6.x) |
| MEDIUM     | Possivel problema, precisa validacao (Alpine < 3.14, .NET < 6)            |
| LOW        | Risco baixo ou metadados insuficientes para validacao                     |
| INFO       | Finding informativo                                                       |
| OK         | Sem problemas detectados (metadados validados)                            |
| UNKNOWN    | Nao inspecionado (skopeo desabilitado ou falhou)                          |


Imagens encontradas apenas em initContainers tem a severidade reduzida em um nivel.

## Arquitetura

```
Browser  -->  Flask (Gunicorn, PatternFly v6 dark theme)
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

---

## setup.sh -- Referencia Completa

O `setup.sh` e o script unico de gerenciamento de todo o ciclo de vida da aplicacao: desenvolvimento local, build da imagem, push para registries, deploy no OpenShift e operacoes do dia-a-dia.

```
Uso: ./setup.sh [opcoes globais] <acao> [<acao> ...]
```

Multiplas acoes podem ser combinadas em um unico comando e sao executadas em ordem.

### Opcoes Globais

As opcoes globais devem vir **antes** das acoes. Elas configuram como as acoes se comportam.


| Opcao                 | Argumento                  | Descricao                                                       |
| --------------------- | -------------------------- | --------------------------------------------------------------- |
| `--proxy <URL|auto>`  | URL do proxy ou `auto`     | Configura proxy HTTP/HTTPS para build, push e deployment        |
| `--no-proxy <hosts>`  | Lista separada por virgula | Override de hosts NO_PROXY (mesclado com CIDRs auto-detectados) |
| `--mirror`            | *(nenhum)*                 | Envia imagem para o registro interno do OCP ao inves do Quay.io |
| `--ca-cert <arquivo>` | Caminho para PEM           | Certificado CA customizado para proxies que interceptam TLS     |


### Acoes

#### Desenvolvimento Local


| Acao          | Descricao                                                           |
| ------------- | ------------------------------------------------------------------- |
| `--build`     | Build da imagem container com Podman                                |
| `--run-local` | Rodar o container localmente em `localhost:8080`                    |
| `--stop`      | Parar e remover o container local                                   |
| `--status`    | Mostrar status e saude do container local                           |
| `--logs`      | Acompanhar logs do container local (Ctrl+C para sair)               |
| `--destroy`   | Remover container, imagem e todos os dados locais (com confirmacao) |


#### Deploy no OpenShift


| Acao                  | Descricao                                                                                                                                       |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `--push`              | Push da imagem para Quay.io                                                                                                                     |
| `--deploy`            | Aplicar manifests no OpenShift (namespace, RBAC, deployment, service, route). Popula credenciais de registry automaticamente no primeiro deploy |
| `--build-push-deploy` | Pipeline completa: build + push (ou mirror) + deploy + restart                                                                                  |
| `--restart`           | Rolling restart do deployment                                                                                                                   |
| `--persistent`        | Persistir credenciais de registry em um Kubernetes Secret (sobrevive a restarts de pod)                                                         |
| `--openshift-status`  | Mostrar deployments, pods, services e routes                                                                                                    |
| `--openshift-logs`    | Acompanhar logs do deployment (Ctrl+C para sair)                                                                                                |
| `--remove`            | Remover completamente a aplicacao do OpenShift (com confirmacao)                                                                                |


---

### Como funciona o `--proxy`

A opcao `--proxy` configura proxy HTTP/HTTPS para todas as fases do pipeline.

**Com URL explicita** (`--proxy http://proxy.corp.com:8080`):


| Fase       | Efeito                                                                                                 |
| ---------- | ------------------------------------------------------------------------------------------------------ |
| `--build`  | Passa `http_proxy` / `https_proxy` como `--build-arg` para `podman build`                              |
| `--push`   | Exporta `HTTP_PROXY` / `HTTPS_PROXY` para `podman push`                                                |
| `--deploy` | Injeta `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (+ variantes minusculas) no Deployment via `oc set env` |


**Com `auto`** (`--proxy auto`):

1. Le o objeto Proxy do cluster (`oc get proxy cluster`)
2. Extrai `spec.httpProxy`, `spec.httpsProxy` e `spec.noProxy`
3. Auto-detecta o **CIDR do service network** (de `network.config/cluster` ou fallback do IP do Service `kubernetes`) e o **CIDR do pod network**
4. Mescla todas as fontes de NO_PROXY (cluster `noProxy` + CIDRs detectados + entradas essenciais como `.cluster.local`, `.svc`, `localhost`)
5. Usa o resultado mesclado para impedir que trafego intra-cluster passe pelo proxy

**Auto-deteccao de NO_PROXY** garante que o API server do Kubernetes e sempre acessivel diretamente, mesmo que o `spec.noProxy` do cluster nao inclua explicitamente o CIDR do service network. Isso previne timeouts de conexao de varios minutos quando o proxy nao consegue alcancar IPs internos do cluster.

### Como funciona o `--no-proxy`

Define explicitamente os hosts NO_PROXY. A lista fornecida e **mesclada** (nao substituida) com:

- CIDRs auto-detectados do cluster (service network, pod network)
- Entradas essenciais (`.cluster.local`, `.svc`, `localhost`, `127.0.0.1`)

Isso garante que a conectividade intra-cluster nunca e quebrada, independente do que voce especificar.

### Como funciona o `--mirror`

Envia a imagem para o **registro interno** do OCP ao inves do Quay.io. Essencial em ambientes onde o cluster nao consegue puxar imagens de registries externos (proxy bloqueando, interceptacao TLS, redes air-gapped, restricoes de registry).

Passos realizados:

1. Garante que o namespace existe
2. Expoe a rota do registro interno (faz patch em `configs.imageregistry.operator.openshift.io/cluster` se necessario)
3. Faz login no registro interno usando o token da sessao `oc` atual (proxy ignorado)
4. Tag e push da imagem para `image-registry.openshift-image-registry.svc:5000/<namespace>/<app>` (proxy ignorado)
5. No `--deploy`, faz patch no Deployment para usar a referencia da imagem interna

Todas as operacoes de mirror limpam explicitamente as variaveis de proxy para garantir conexao direta.

### Como funciona o `--ca-cert`

Injeta um certificado CA customizado na imagem durante o build. Necessario quando um proxy corporativo faz interceptacao TLS -- o CA do proxy precisa ser confiavel dentro do container para o `skopeo` funcionar corretamente.

O certificado e copiado como `.build-ca.pem` durante o `podman build` e removido em seguida.

### Credenciais de registry auto-populadas

No primeiro `--deploy`, o script automaticamente:

1. Le o pull-secret global do cluster (`openshift-config/pull-secret`)
2. Extrai credenciais de registries (registry.redhat.io, quay.io, etc.)
3. Cria um Kubernetes Secret (`cgroups-v2-checker-registries`) com `registries.json`
4. Faz patch no Deployment para montar o Secret e define a env var `REGISTRIES_FILE`

Isso da ao scanner acesso imediato a todos os registries configurados no cluster, sem necessidade de configuracao manual.

- Requer acesso de leitura ao `openshift-config/pull-secret` (normalmente cluster-admin)
- Ignorado se o Secret de credenciais ja existir (preserva credenciais adicionadas pelo usuario)
- Registries adicionais podem ser adicionados pela interface web e persistidos com `--persistent`

### Como funciona o `--persistent`

Persiste credenciais de registry em um Kubernetes Secret para sobreviver a restarts de pod:

1. Le `registries.json` do pod em execucao (credenciais adicionadas pela interface web)
2. Cria ou atualiza o Secret `cgroups-v2-checker-registries`
3. Faz patch no Deployment para montar o Secret
4. Reinicia o Deployment

Se nao encontrar pod em execucao, oferece entrada interativa manual de credenciais.

---

### Cenarios de Deploy -- Qual Combinacao Usar

#### Acesso direto a internet (sem proxy)

O caso mais simples. O cluster consegue puxar do Quay.io e o skopeo alcanca todos os registries.

```bash
./setup.sh --deploy
```

#### Proxy corporativo, cluster consegue puxar do Quay.io

O cluster puxa a imagem do app do Quay.io normalmente, mas o skopeo precisa do proxy para alcancar registries externos na inspecao.

```bash
# Auto-detectar proxy da configuracao do cluster
./setup.sh --proxy auto --build-push-deploy

# Ou especificar a URL do proxy
./setup.sh --proxy http://proxy.corp.com:8080 --build-push-deploy
```

#### Proxy corporativo, cluster NAO consegue puxar do Quay.io

O cluster nao alcanca o Quay.io (proxy bloqueia, interceptacao TLS, etc.). A imagem e enviada para o registro interno.

```bash
# Mirror + auto-detectar proxy (mais comum em ambientes enterprise)
./setup.sh --mirror --proxy auto --build-push-deploy
```

#### Proxy com interceptacao TLS e CA customizado

O proxy faz inspecao TLS e substitui certificados. O skopeo vai falhar sem o certificado CA do proxy.

```bash
# Mirror + proxy + CA customizado
./setup.sh --mirror --proxy auto --ca-cert /path/to/proxy-ca.pem --build-push-deploy

# Sem mirror (se o cluster consegue puxar do Quay.io)
./setup.sh --proxy auto --ca-cert /path/to/proxy-ca.pem --build-push-deploy
```

#### Ambiente air-gapped / desconectado

Sem acesso a rede externa. Build da imagem em uma maquina conectada, transfere, depois faz deploy.

```bash
# Na maquina conectada: build e salvar
./setup.sh --build
podman save quay.io/chiaretto/cgroups-v2-checker:latest -o cgroups-v2-checker.tar

# No cluster desconectado: carregar e deploy com mirror
podman load -i cgroups-v2-checker.tar
./setup.sh --mirror --deploy --restart
```

#### Atualizando apos mudancas no codigo

```bash
# Rebuild e redeploy (preserva configuracoes de proxy/mirror existentes no deployment)
./setup.sh --build-push-deploy

# Ou com mirror + proxy
./setup.sh --mirror --proxy auto --build-push-deploy

# Apenas restart rapido (sem rebuild)
./setup.sh --restart
```

#### Persistir credenciais de registry apos configuracao pela web UI

```bash
# Apos adicionar credenciais pela interface web:
./setup.sh --persistent
```

#### Passos individuais (controle granular)

```bash
./setup.sh --build                          # Apenas build
./setup.sh --push                           # Apenas push para Quay.io
./setup.sh --mirror --deploy                # Mirror + deploy (sem build)
./setup.sh --proxy auto --deploy            # Deploy com injecao de proxy apenas
./setup.sh --deploy --restart               # Aplicar manifests + restart
./setup.sh --openshift-status               # Verificar status do deploy
./setup.sh --openshift-logs                 # Acompanhar logs do pod
./setup.sh --remove                         # Desinstalar tudo
```

#### Limpeza completa e reinstalacao

```bash
# Remover do OpenShift
./setup.sh --remove

# Remover recursos locais
./setup.sh --destroy

# Instalacao nova
./setup.sh --mirror --proxy auto --build-push-deploy
```

---

### Opcoes Globais vs Acoes -- Referencia Rapida

```
./setup.sh [--proxy URL|auto] [--no-proxy hosts] [--mirror] [--ca-cert arquivo] <acao>
           \_________________/ \________________/ \________/ \________________/
           Opcional: proxy      Opcional: hosts     Opcional:  Opcional:
           para build/push/     NO_PROXY extras     push para  CA customizado
           deploy                                   registro   para interceptacao
                                                    interno    TLS
```

**Matriz de combinacoes:**


| Cenario                     | `--proxy`     | `--mirror`  | `--ca-cert` | `--no-proxy` |
| --------------------------- | ------------- | ----------- | ----------- | ------------ |
| Internet direta             | -             | -           | -           | -            |
| Proxy, pull externo OK      | `auto` ou URL | -           | -           | opcional     |
| Proxy, sem pull externo     | `auto` ou URL | sim         | -           | opcional     |
| Proxy com interceptacao TLS | `auto` ou URL | recomendado | sim         | opcional     |
| Air-gapped                  | -             | sim         | -           | -            |


---

## Desenvolvimento Local

```bash
pip install -r requirements.txt
export REPORT_DIR=./data/reports
python run.py
```

Acesse `http://localhost:8080`. A conexao ao cluster usa o kubeconfig local (`~/.kube/config` ou `$KUBECONFIG`).

### Rodar localmente com Podman

```bash
./setup.sh --build --run-local    # Build e rodar
./setup.sh --status               # Verificar status
./setup.sh --logs                 # Ver logs
./setup.sh --stop                 # Parar
./setup.sh --destroy              # Remover tudo
```

## Variaveis de Ambiente


| Variavel                 | Padrao                   | Descricao                                           |
| ------------------------ | ------------------------ | --------------------------------------------------- |
| `SECRET_KEY`             | (gerado automaticamente) | Chave secreta do Flask                              |
| `REPORT_DIR`             | `/app/data/reports`      | Diretorio para salvar relatorios                    |
| `SKIP_SYSTEM_NAMESPACES` | `true`                   | Pular namespaces de sistema do OpenShift            |
| `SKOPEO_TLS_VERIFY`      | `true`                   | Verificar TLS nas chamadas do skopeo                |
| `SKOPEO_AUTH_FILE`       | (vazio)                  | Arquivo de autenticacao para registries privados    |
| `SKOPEO_MAX_WORKERS`     | `20`                     | Threads paralelas para inspecao via skopeo          |
| `EXEC_MAX_WORKERS`       | `10`                     | Threads paralelas para exec checks em pods          |
| `USE_IMAGE_PULL_SECRETS` | `true`                   | Extrair auth de registries dos pods/ServiceAccounts |
| `REGISTRIES_FILE`        | (vazio)                  | Caminho para registries.json montado de K8s Secret  |
| `IMAGE_TAG`              | `latest`                 | Tag da imagem usada pelo setup.sh                   |
| `LOCAL_PORT`             | `8080`                   | Porta local para `--run-local`                      |


## API REST


| Metodo   | Endpoint                 | Descricao                         |
| -------- | ------------------------ | --------------------------------- |
| `POST`   | `/api/scan`              | Iniciar nova varredura            |
| `GET`    | `/api/scan/<id>`         | Status e progresso da varredura   |
| `GET`    | `/api/reports`           | Listar todos os relatorios        |
| `GET`    | `/api/reports/<id>`      | Obter relatorio especifico        |
| `DELETE` | `/api/reports/<id>`      | Remover relatorio                 |
| `GET`    | `/api/registries`        | Listar credenciais de registry    |
| `POST`   | `/api/registries`        | Adicionar credenciais de registry |
| `DELETE` | `/api/registries/<host>` | Remover credenciais de registry   |


### Exemplo: Iniciar varredura

```bash
curl -X POST http://cgroups-v2-checker-route/api/scan \
  -H "Content-Type: application/json" \
  -d '{"namespaces": ["my-app"], "inspect_images": true, "exec_check": true}'
```

### Opcoes de varredura


| Campo                | Tipo | Padrao | Descricao                                         |
| -------------------- | ---- | ------ | ------------------------------------------------- |
| `namespaces`         | list | todos  | Namespaces especificos para varrer                |
| `exclude_namespaces` | list | nenhum | Namespaces para excluir                           |
| `namespace_patterns` | list | nenhum | Padroes regex para incluir namespaces             |
| `exclude_patterns`   | list | nenhum | Padroes regex para excluir namespaces             |
| `inspect_images`     | bool | true   | Habilitar inspecao via skopeo (Nivel 2)           |
| `exec_check`         | bool | false  | Habilitar deteccao runtime via pod exec (Nivel 3) |


## Imagem Container

A aplicacao roda em UBI 9 com Python 3.11 e skopeo pre-instalados. Construida com Podman e implantavel em qualquer cluster OpenShift 4.x.

## Licenca

Este projeto e fornecido como esta para avaliacao de prontidao de upgrade do OpenShift.