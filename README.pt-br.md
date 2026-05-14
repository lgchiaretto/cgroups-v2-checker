# cgroups v2 Checker -- Documentacao

Aplicacao web para varrer clusters OpenShift e identificar imagens de container com problemas de compatibilidade com cgroups v2 antes do upgrade para o OpenShift 4.19.

Executa um script de deteccao leve dentro de cada pod em execucao via `oc exec` para verificar versao do SO, runtimes (Java, Node.js, .NET), flags JVM e referencias a cgroups v1.


| [English Documentation](README.md) |
| ---------------------------------- |


---

## Contexto

O OpenShift 4.19 utiliza RHCOS 9, que opera exclusivamente com cgroups v2 (unified hierarchy). Imagens baseadas em sistemas operacionais antigos (RHEL 7, CentOS 7, Ubuntu < 20.04, etc.) ou com runtimes desatualizados (JDK < 15, .NET < 6) podem apresentar falhas criticas no novo ambiente.

Esta ferramenta ajuda administradores de cluster a identificar essas imagens antes do upgrade, fornecendo findings classificados por severidade e recomendacoes de correcao.

## Funcionalidades

- Deteccao via pod exec: SO, Java, Node.js, .NET, flags JVM, referencias a cgroups v1
- Classificacao de severidade: CRITICAL, HIGH, LOW, INFO, OK, UNKNOWN
- Trilha de auditoria de inspecao (mostra o que foi detectado para cada imagem)
- Cards de severidade clicaveis para filtrar a tabela de resultados
- Modal de drilldown por imagem mostrando pods e namespaces
- Paginacao para listas grandes (20/50/100/Todos)
- Filtro por texto e botoes de filtro por severidade
- Download de relatorio em CSV e JSON
- Detalhamento de pods excluidos (namespaces de sistema vs. excluidos pelo usuario)
- Scan em background com progresso em tempo real
- Tema escuro PatternFly 6 (estilo OpenShift Console)

## Como Funciona

### Inspecao via Pod Exec (primaria)

Lista todos os pods do cluster via API do Kubernetes e executa um script de deteccao leve dentro de cada pod em execucao via `oc exec`. O script e somente leitura e usa `command -v` antes de executar comandos de runtime.

Deteccoes realizadas:

- **Sistema Operacional**: le `/etc/os-release` para detectar SO legado (CentOS 7, RHEL 7, Ubuntu < 20.04, Debian < 11, Alpine < 3.14)
- **Java**: executa `java -version` para obter a versao real do JDK (seguro: 17+, 11.0.16+, 8u372+)
- **Flags JVM**: verifica `JAVA_TOOL_OPTIONS`, `JAVA_OPTS`, `_JAVA_OPTIONS`, `JDK_JAVA_OPTIONS` por `-XX:-UseContainerSupport`
- **Node.js**: executa `node --version` (seguro: 20+)
- **.NET**: executa `dotnet --list-runtimes` (seguro: 5+)
- **Hierarquia cgroups**: verifica se o pod roda sob cgroups v1 ou v2
- **Referencias a arquivos cgroups v1**: busca em arquivos da aplicacao por paths v1 hardcoded (`memory.limit_in_bytes`, `cpu.cfs_quota_us`, etc.)
- **Referencias cgroups v1 em ENV**: verifica variaveis de ambiente do PID 1 por paths v1

### Classificacao de Severidade

#### CRITICAL -- SO base incompativel, vai quebrar no cgroups v2

O sistema operacional nao suporta cgroups v2. Esses containers **vao falhar** apos o upgrade para OCP 4.19.

Exemplos reais:
- `centos:7`, `quay.io/centos/centos:7` -- CentOS 7 (EOL junho 2024), kernel e ferramentas userspace so entendem cgroups v1
- `registry.access.redhat.com/ubi7/ubi:latest` -- Imagens baseadas em UBI 7 / RHEL 7
- `registry.access.redhat.com/rhel6/rhel:latest` -- Imagens baseadas em RHEL 6
- `ubuntu:18.04`, `ubuntu:16.04` -- Versoes do Ubuntu anteriores a 20.04
- `debian:10` (Buster), `debian:9` (Stretch) -- Versoes do Debian anteriores a 11
- `openjdk:8u302-jre-slim` -- Java 8 < 8u372 (nao consegue ler limites de memoria/CPU do container sob cgroups v2)

**Acao necessaria**: Reconstruir a aplicacao em uma imagem base compativel (UBI 8/9, Ubuntu 22.04+, Debian 12+) **antes** do upgrade.

#### HIGH -- Runtime precisa de atualizacao, pode causar problemas no cgroups v2

O SO base e compativel, mas o runtime da aplicacao tem problemas conhecidos com cgroups v2. O container vai iniciar, mas a aplicacao pode **se comportar incorretamente** (limites de memoria errados, throttling de CPU, OOM kills).

Exemplos reais:
- `openjdk:11.0.11-jre-slim` -- Java 11 < 11.0.16 le paths de cgroups v1 para memoria/CPU, obtem valores do host em vez dos limites do container
- `registry.access.redhat.com/ubi8/nodejs-16:latest` -- Node.js 16 tem suporte limitado a cgroups v2 para dimensionamento de heap
- `registry.access.redhat.com/ubi9/nodejs-18:latest` -- Node.js 18 tem suporte parcial a cgroups v2, deve atualizar para 20+
- Alpine 3.13 ou anterior com runtimes -- musl libc < 1.2.2 tem suporte incompleto a cgroups v2
- Java 17 com `JAVA_TOOL_OPTIONS="-XX:-UseContainerSupport"` -- desabilita explicitamente a deteccao de container, JVM ignora limites de cgroups
- Arquivos da aplicacao referenciando paths cgroups v1 hardcoded (`/sys/fs/cgroup/memory/memory.limit_in_bytes`, `/sys/fs/cgroup/cpu/cpu.cfs_quota_us`)
- Variaveis de ambiente apontando para paths de cgroups v1

**Acao necessaria**: Atualizar o runtime para uma versao compativel com cgroups v2 ou corrigir a configuracao. Versoes seguras: Java 17+ / 11.0.16+ / 8u372+, Node.js 20+, .NET 6+.

#### LOW -- Risco menor, revisar quando conveniente

Findings de baixo risco ou situacoes onde os metadados sao insuficientes para uma avaliacao definitiva.

Exemplos reais:
- Metadados insuficientes para determinar compatibilidade completa
- Itens de configuracao menores que dificilmente causarao falhas

**Acao**: Revisar quando conveniente. Esses findings dificilmente causarao problemas imediatos durante o upgrade.

#### INFO -- Informativo, nenhuma acao necessaria

Findings puramente informativos que fornecem contexto mas nao indicam risco.

**Acao**: Nenhuma acao necessaria.

#### OK -- Totalmente compativel com cgroups v2

A imagem foi inspecionada e nenhum problema de compatibilidade foi encontrado.

Exemplos reais:
- `registry.access.redhat.com/ubi9/openjdk-17:latest` -- Java 17 no UBI 9
- `registry.access.redhat.com/ubi9/openjdk-21:latest` -- Java 21 no UBI 9
- `registry.access.redhat.com/ubi8/openjdk-8:1.18` -- Java 8u392+ (build seguro)
- `registry.access.redhat.com/ubi9/nodejs-20:latest` -- Node.js 20 no UBI 9
- `registry.access.redhat.com/ubi9/ubi-minimal:latest` -- UBI 9 minimal (sem runtime)
- `registry.access.redhat.com/ubi8/ubi-minimal:latest` -- UBI 8 minimal

#### UNKNOWN -- Nao foi possivel inspecionar

O checker nao conseguiu inspecionar a imagem. Geralmente porque o `oc exec` falhou (o pod crashou, o container nao tem shell, ou permissoes foram negadas).

Exemplos reais:
- `gcr.io/distroless/java17-debian11` -- Imagens distroless nao tem shell, exec nao consegue executar
- Pods em estado `CrashLoopBackOff` ou `Error`
- Containers com `securityContext.readOnlyRootFilesystem` e permissoes de exec restritas

**Acao**: Verificar essas imagens manualmente.

---

Imagens encontradas apenas em initContainers sao sinalizadas no relatorio mas recebem a mesma severidade que containers regulares -- se a imagem tem problema, precisa ser corrigida independente de onde roda.

## Arquitetura

```
Browser  -->  Flask (Gunicorn, PatternFly v6 dark theme)
                |
                +--> Kubernetes API (list pods, get cluster info)
                +--> pod exec (SO, runtimes, deteccao cgroups)
                +--> JSON reports salvos em /app/data/reports/
```

A aplicacao roda dentro do cluster OpenShift com uma ServiceAccount que tem permissao para listar pods, executar comandos em pods e ler informacoes do cluster.

## Estrutura de Arquivos

```
app/
  app.py             Flask application factory
  config.py          Configuracao via variaveis de ambiente
  scanner.py         Motor de varredura (Kubernetes API + pod exec)
  routes.py          Rotas web (dashboard, relatorios, CSV)
  api.py             API REST (/api/scan, /api/reports)
  templates/         Templates Jinja2 (PatternFly v6 dark theme)
    base.html        Layout base com navegacao lateral
    dashboard.html   Pagina inicial com controles de scan
    report.html      Visualizacao de relatorio com cards, filtros, paginacao
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
Containerfile        Build da imagem (UBI 9 + Python 3.12)
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
| `--deploy`            | Aplicar manifests no OpenShift (namespace, RBAC, deployment, service, route)  |
| `--build-push-deploy` | Pipeline completa: build + push (ou mirror) + deploy + restart               |
| `--restart`           | Rolling restart do deployment                                                |
| `--openshift-status`  | Mostrar deployments, pods, services e routes                                 |
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

Injeta um certificado CA customizado na imagem durante o build. Necessario quando um proxy corporativo faz interceptacao TLS -- o CA do proxy precisa ser confiavel dentro do container para pip installs e outras operacoes de rede durante o build.

O certificado e copiado como `.build-ca.pem` durante o `podman build` e removido em seguida.

---

### Cenarios de Deploy -- Qual Combinacao Usar

#### Acesso direto a internet (sem proxy)

O caso mais simples. O cluster consegue puxar do Quay.io diretamente.

```bash
./setup.sh --deploy
```

#### Proxy corporativo, cluster consegue puxar do Quay.io

O cluster puxa a imagem do app do Quay.io normalmente, mas precisa do proxy para acesso externo.

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

O proxy faz inspecao TLS e substitui certificados. O CA customizado precisa ser injetado na imagem para pip installs durante o build.

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
| `SECRET_KEY`             | (gerado automaticamente) | Chave secreta do Flask                       |
| `REPORT_DIR`             | `/app/data/reports`      | Diretorio para salvar relatorios             |
| `SKIP_SYSTEM_NAMESPACES` | `true`                   | Pular namespaces de sistema do OpenShift     |
| `EXEC_MAX_WORKERS`       | `20`                     | Threads paralelas para inspecao via pod exec |
| `IMAGE_TAG`              | `latest`                 | Tag da imagem usada pelo setup.sh            |
| `LOCAL_PORT`             | `8080`                   | Porta local para `--run-local`               |


## API REST


| Metodo   | Endpoint                 | Descricao                         |
| -------- | ------------------------ | --------------------------------- |
| `POST`   | `/api/scan`         | Iniciar nova varredura          |
| `GET`    | `/api/scan/<id>`    | Status e progresso da varredura |
| `GET`    | `/api/reports`      | Listar todos os relatorios      |
| `GET`    | `/api/reports/<id>` | Obter relatorio especifico      |
| `DELETE` | `/api/reports/<id>` | Remover relatorio               |


### Exemplo: Iniciar varredura

```bash
curl -X POST http://cgroups-v2-checker-route/api/scan \
  -H "Content-Type: application/json" \
  -d '{"namespaces": ["my-app"]}'
```

### Opcoes de varredura


| Campo                | Tipo | Padrao | Descricao                                                    |
| -------------------- | ---- | ------ | ------------------------------------------------------------ |
| `namespaces`         | list | todos  | Namespaces especificos para varrer    |
| `exclude_namespaces` | list | nenhum | Namespaces para excluir               |
| `namespace_patterns` | list | nenhum | Padroes regex para incluir namespaces |
| `exclude_patterns`   | list | nenhum | Padroes regex para excluir namespaces |


## Imagem Container

A aplicacao roda em UBI 9 com Python 3.12. Construida com Podman e implantavel em qualquer cluster OpenShift 4.x.

## Licenca

Este projeto e fornecido como esta para avaliacao de prontidao de upgrade do OpenShift.