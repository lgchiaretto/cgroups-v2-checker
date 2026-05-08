#!/bin/bash
#
# cgroups v2 Checker - Setup Script
#
# This script helps you build, push, and deploy the cgroups-v2-checker
# application using Podman and OpenShift CLI.
#
# Usage:
#   ./setup.sh [global options] <action>
#
# Global Options:
#   --proxy <URL|auto>       Set HTTP/HTTPS proxy (used in build, push,
#                            and injected into the OpenShift deployment).
#                            Use "auto" to read from cluster Proxy object.
#   --no-proxy <hosts>       Comma-separated NO_PROXY hosts (optional,
#                            auto-detected from cluster or uses defaults)
#   --mirror                 Push image to the OCP internal registry
#                            instead of Quay.io (avoids proxy/TLS issues)
#   --ca-cert <file>         Custom CA certificate PEM for TLS-
#                            intercepting proxies. Injected into build.
#
# Actions:
#   Local Development:
#     --build                Build the container image
#     --run-local            Run locally with Podman
#     --stop                 Stop the local container
#     --status               Check local container status
#     --logs                 Show local container logs
#     --destroy              Remove container, image, and data
#
#   OpenShift Deployment:
#     --push                 Push image to Quay.io
#     --deploy               Deploy to OpenShift (apply manifests).
#                            On first deploy, auto-seeds registry
#                            credentials from the cluster pull-secret
#                            (registry.redhat.io, quay.io, etc.).
#     --build-push-deploy    Full pipeline: build, push, deploy, restart
#     --restart              Restart the OpenShift deployment
#     --persistent           Persist registry credentials to a K8s Secret
#     --openshift-status     Show OpenShift resources status
#     --openshift-logs       Tail OpenShift deployment logs
#     --remove               Completely remove app from OpenShift
#
#   --help                   Show this help message
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load centralized secrets if available
[[ -f ~/.openshift-lab-secrets.env ]] && source ~/.openshift-lab-secrets.env

# Configuration
APP_NAME="cgroups-v2-checker"
CONTAINER_NAME="${APP_NAME}"
IMAGE_NAME="quay.io/chiaretto/${APP_NAME}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_REF="${IMAGE_NAME}:${IMAGE_TAG}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
NAMESPACE="${APP_NAME}"

# Proxy settings (set via --proxy flag)
PROXY_URL=""
NO_PROXY_HOSTS=""
ESSENTIAL_NO_PROXY=".cluster.local,.svc,localhost,127.0.0.1"

# Mirror mode: push to internal registry instead of quay.io
MIRROR_MODE=false
INTERNAL_IMAGE_REF=""

# Custom CA certificate for TLS-intercepting proxies
CUSTOM_CA_PATH=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Show help
show_help() {
    head -42 "$0" | tail -40
    exit 0
}

# Detect Kubernetes/OpenShift service and pod CIDRs for NO_PROXY
detect_cluster_cidrs() {
    local cidrs=""

    # Service CIDR: read from Kubernetes API server IP or network config
    local api_ip
    api_ip=$(oc get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
    if [[ -n "$api_ip" ]]; then
        local svc_cidr
        svc_cidr=$(oc get network.config cluster -o jsonpath='{.status.serviceNetwork[0]}' 2>/dev/null)
        if [[ -n "$svc_cidr" ]]; then
            cidrs="${svc_cidr}"
        else
            # Fallback: use /16 from the API server IP
            local base
            base=$(echo "$api_ip" | cut -d. -f1-2)
            cidrs="${base}.0.0/16"
        fi
    fi

    # Pod CIDR
    local pod_cidr
    pod_cidr=$(oc get network.config cluster -o jsonpath='{.status.clusterNetwork[0].cidr}' 2>/dev/null)
    if [[ -n "$pod_cidr" ]]; then
        cidrs="${cidrs:+$cidrs,}${pod_cidr}"
    fi

    echo "$cidrs"
}

# Merge NO_PROXY lists, deduplicating entries
merge_no_proxy() {
    local merged=""
    local seen=""
    for list in "$@"; do
        IFS=',' read -ra items <<< "$list"
        for item in "${items[@]}"; do
            item=$(echo "$item" | xargs)  # trim whitespace
            [[ -z "$item" ]] && continue
            if [[ ",$seen," != *",$item,"* ]]; then
                merged="${merged:+$merged,}${item}"
                seen="${seen:+$seen,}${item}"
            fi
        done
    done
    echo "$merged"
}

# Auto-detect proxy from the OpenShift cluster Proxy object
detect_cluster_proxy() {
    if ! command -v oc &> /dev/null || ! oc whoami &> /dev/null 2>&1; then
        return 1
    fi

    local proxy_json
    proxy_json=$(oc get proxy cluster -o json 2>/dev/null) || return 1

    local http_proxy https_proxy no_proxy
    http_proxy=$(echo "$proxy_json" | python3 -c "import json,sys; p=json.load(sys.stdin).get('spec',{}); print(p.get('httpProxy',''))" 2>/dev/null)
    https_proxy=$(echo "$proxy_json" | python3 -c "import json,sys; p=json.load(sys.stdin).get('spec',{}); print(p.get('httpsProxy',''))" 2>/dev/null)
    no_proxy=$(echo "$proxy_json" | python3 -c "import json,sys; p=json.load(sys.stdin).get('spec',{}); print(p.get('noProxy',''))" 2>/dev/null)

    if [[ -z "$http_proxy" && -z "$https_proxy" ]]; then
        return 1
    fi

    PROXY_URL="${https_proxy:-$http_proxy}"

    # Auto-detect cluster CIDRs (service network, pod network)
    local cluster_cidrs
    cluster_cidrs=$(detect_cluster_cidrs)

    if [[ -z "$NO_PROXY_HOSTS" ]]; then
        # Merge: cluster noProxy + auto-detected CIDRs + essential entries
        NO_PROXY_HOSTS=$(merge_no_proxy "$no_proxy" "$cluster_cidrs" "$ESSENTIAL_NO_PROXY")
    else
        # User provided --no-proxy: merge with CIDRs + essentials
        NO_PROXY_HOSTS=$(merge_no_proxy "$NO_PROXY_HOSTS" "$cluster_cidrs" "$ESSENTIAL_NO_PROXY")
    fi

    log_success "Auto-detected cluster proxy: ${PROXY_URL}"
    log_info "NO_PROXY: ${NO_PROXY_HOSTS}"
    return 0
}

# ============================================================================
# Local Development Functions
# ============================================================================

# Build container image
build_image() {
    log_info "Building ${APP_NAME} container image..."
    local build_args=()
    if [[ -n "$PROXY_URL" ]]; then
        log_info "Using proxy for build: ${PROXY_URL}"
        build_args+=(--build-arg "http_proxy=${PROXY_URL}")
        build_args+=(--build-arg "https_proxy=${PROXY_URL}")
        build_args+=(--build-arg "no_proxy=${NO_PROXY_HOSTS:-$ESSENTIAL_NO_PROXY}")
    fi

    # Always create .build-ca.pem (empty if no CA, real cert if provided)
    if [[ -n "$CUSTOM_CA_PATH" ]]; then
        cp "$CUSTOM_CA_PATH" "${SCRIPT_DIR}/.build-ca.pem"
        log_info "Custom CA certificate: ${CUSTOM_CA_PATH}"
    else
        : > "${SCRIPT_DIR}/.build-ca.pem"
    fi

    podman build "${build_args[@]}" -t "${IMAGE_REF}" -f Containerfile .
    rm -f "${SCRIPT_DIR}/.build-ca.pem"
    log_success "Image built successfully: ${IMAGE_REF}"
}

# Run locally with Podman
run_local() {
    log_info "Starting ${APP_NAME} locally..."

    # Stop existing container if running
    if podman ps -a --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
        log_warn "Container '${CONTAINER_NAME}' already exists. Removing..."
        podman rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    fi

    # Create local data directory
    mkdir -p "${SCRIPT_DIR}/data/reports"

    podman run -d \
        --name "${CONTAINER_NAME}" \
        -p "${LOCAL_PORT}:8080" \
        -e REPORT_DIR=/app/data/reports \
        -e SKIP_SYSTEM_NAMESPACES=true \
        -e SKOPEO_TLS_VERIFY=true \
        -v "${SCRIPT_DIR}/data:/app/data:z" \
        "${IMAGE_REF}"

    log_success "${APP_NAME} started on http://localhost:${LOCAL_PORT}"
    wait_for_service
}

# Wait for service to be ready
wait_for_service() {
    log_info "Waiting for service to be ready..."
    # shellcheck disable=SC2034 - this loop is used for 30 seconds timeout
    for i in {1..30}; do
        if curl -s "http://127.0.0.1:${LOCAL_PORT}/" >/dev/null 2>&1; then
            log_success "Service is ready!"
            return 0
        fi
        sleep 1
    done
    log_warn "Service may not be fully ready. Check logs with: $0 --logs"
}

# Stop the local container
stop_service() {
    log_info "Stopping ${APP_NAME}..."
    podman stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    podman rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    log_success "Service stopped"
}

# Check local status
check_status() {
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  ${APP_NAME} - Local Status"
    echo "═══════════════════════════════════════════════════════════════"

    if podman ps --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
        echo -e "Container:  ${GREEN}Running${NC}"
        if curl -s "http://127.0.0.1:${LOCAL_PORT}/" >/dev/null 2>&1; then
            echo -e "Health:     ${GREEN}Healthy${NC}"
        else
            echo -e "Health:     ${RED}Unhealthy${NC}"
        fi
    else
        echo -e "Container:  ${RED}Not Running${NC}"
    fi

    echo ""
    echo "URL:   http://localhost:${LOCAL_PORT}"
    echo "Image: ${IMAGE_REF}"
    echo ""
}

# Show local logs
show_logs() {
    log_info "Showing ${APP_NAME} logs (Ctrl+C to exit)..."
    podman logs -f "${CONTAINER_NAME}"
}

# Destroy everything locally
destroy_everything() {
    log_warn "═══════════════════════════════════════════════════════════════"
    log_warn "  COMPLETE LOCAL CLEANUP"
    log_warn "═══════════════════════════════════════════════════════════════"
    echo ""
    log_warn "This will PERMANENTLY DELETE:"
    echo "  - Container: ${CONTAINER_NAME}"
    echo "  - Container image: ${IMAGE_REF}"
    echo "  - Local data directory: ${SCRIPT_DIR}/data"
    echo ""
    read -p "Type 'yes' to confirm: " -r
    echo

    if [[ ! "$REPLY" == "yes" ]]; then
        log_info "Cancelled."
        exit 0
    fi

    # Stop and remove container
    if podman ps -a --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
        log_info "[1/3] Removing container..."
        podman rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        log_success "Container removed"
    else
        log_info "[1/3] Container not found (skipping)"
    fi

    # Remove local data
    if [ -d "${SCRIPT_DIR}/data" ]; then
        log_info "[2/3] Removing local data..."
        rm -rf "${SCRIPT_DIR}/data"
        log_success "Data removed"
    else
        log_info "[2/3] Data directory not found (skipping)"
    fi

    # Remove image
    if podman images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${IMAGE_REF}$"; then
        log_info "[3/3] Removing container image..."
        podman rmi -f "${IMAGE_REF}" >/dev/null 2>&1 || true
        log_success "Image removed"
    else
        log_info "[3/3] Image not found (skipping)"
    fi

    echo ""
    log_success "Cleanup complete. Reinstall with: $0 --build && $0 --run-local"
}

# ============================================================================
# OpenShift Deployment Functions
# ============================================================================

# Push image to registry
push_image() {
    log_info "Pushing image to Quay.io..."
    if [[ -n "$PROXY_URL" ]]; then
        log_info "Using proxy for push: ${PROXY_URL}"
        HTTPS_PROXY="$PROXY_URL" HTTP_PROXY="$PROXY_URL" \
            NO_PROXY="${NO_PROXY_HOSTS:-$ESSENTIAL_NO_PROXY}" \
            podman push "${IMAGE_REF}"
    else
        podman push "${IMAGE_REF}"
    fi
    log_success "Image pushed: ${IMAGE_REF}"
}

# Push image to the OCP internal registry (bypasses proxy/TLS issues)
mirror_to_internal() {
    log_info "Mirroring image to OpenShift internal registry..."

    if ! command -v oc &> /dev/null; then
        log_error "OpenShift CLI (oc) not found."
        exit 1
    fi
    if ! oc whoami &> /dev/null; then
        log_error "Not logged into OpenShift. Please run 'oc login' first."
        exit 1
    fi

    # Ensure the namespace exists
    oc apply -f openshift/namespace.yaml 2>/dev/null || true

    # Get the external route for the internal registry
    local registry_route
    registry_route=$(oc get route default-route -n openshift-image-registry \
        -o jsonpath='{.spec.host}' 2>/dev/null || true)

    if [[ -z "$registry_route" ]]; then
        log_info "Internal registry route not found. Exposing it..."
        oc patch configs.imageregistry.operator.openshift.io/cluster \
            --type merge -p '{"spec":{"defaultRoute":true}}' 2>/dev/null || true
        sleep 5
        registry_route=$(oc get route default-route -n openshift-image-registry \
            -o jsonpath='{.spec.host}' 2>/dev/null || true)
    fi

    if [[ -z "$registry_route" ]]; then
        log_error "Could not get internal registry route. Expose it manually:"
        echo "  oc patch configs.imageregistry.operator.openshift.io/cluster --type merge -p '{\"spec\":{\"defaultRoute\":true}}'"
        exit 1
    fi

    log_info "Internal registry: ${registry_route}"

    # Login to the internal registry using the OC token
    local oc_token
    oc_token=$(oc whoami -t)
    log_info "Logging into internal registry..."
    HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
        NO_PROXY="*" no_proxy="*" \
        podman login --tls-verify=false -u "$(oc whoami)" -p "${oc_token}" "${registry_route}"

    # Tag and push to internal registry
    INTERNAL_IMAGE_REF="${registry_route}/${NAMESPACE}/${APP_NAME}:${IMAGE_TAG}"
    local svc_image_ref="image-registry.openshift-image-registry.svc:5000/${NAMESPACE}/${APP_NAME}:${IMAGE_TAG}"

    log_info "Tagging: ${IMAGE_REF} → ${INTERNAL_IMAGE_REF}"
    podman tag "${IMAGE_REF}" "${INTERNAL_IMAGE_REF}"

    log_info "Pushing to internal registry (proxy bypassed)..."
    HTTP_PROXY="" HTTPS_PROXY="" http_proxy="" https_proxy="" \
        NO_PROXY="*" no_proxy="*" \
        podman push --tls-verify=false "${INTERNAL_IMAGE_REF}"

    log_success "Image mirrored to internal registry"
    log_info "Internal ref: ${svc_image_ref}"

    # Store the svc-internal ref for deploy to use
    INTERNAL_IMAGE_REF="${svc_image_ref}"
}

# Auto-populate registry credentials from the cluster's global pull secret.
# Extracts auths from openshift-config/pull-secret and creates a K8s Secret
# with registries.json so skopeo can authenticate to registry.redhat.io, quay.io, etc.
seed_cluster_registries() {
    local secret_name="${APP_NAME}-registries"

    # Skip if Secret already exists (preserves user-added credentials)
    if oc get secret "${secret_name}" -n ${NAMESPACE} &>/dev/null; then
        log_info "Registry credentials Secret already exists — skipping auto-seed"
        return 0
    fi

    log_info "Auto-populating registry credentials from cluster pull-secret..."

    local pull_secret_b64
    pull_secret_b64=$(oc get secret pull-secret -n openshift-config \
        -o jsonpath='{.data.\.dockerconfigjson}' 2>/dev/null) || {
        log_warn "Could not read cluster pull-secret (need cluster-admin). Skipping auto-seed."
        return 0
    }

    if [[ -z "$pull_secret_b64" ]]; then
        log_warn "Cluster pull-secret is empty. Skipping auto-seed."
        return 0
    fi

    local reg_file="/tmp/${APP_NAME}-seed-registries.json"

    echo "$pull_secret_b64" | base64 -d | python3 -c "
import json, sys, base64
data = json.load(sys.stdin)
auths = data.get('auths', {})
# Registries to skip (telemetry/non-image endpoints)
skip = {'cloud.openshift.com', 'openshift.org'}
result = []
for registry, creds in auths.items():
    if registry in skip:
        continue
    auth = creds.get('auth', '')
    if not auth:
        continue
    try:
        decoded = base64.b64decode(auth).decode()
        user, passwd = decoded.split(':', 1)
        result.append({'registry': registry, 'username': user, 'password': passwd})
    except Exception:
        continue
print(json.dumps(result, indent=2))
" > "${reg_file}" 2>/dev/null

    local count
    count=$(python3 -c "import json; print(len(json.load(open('${reg_file}'))))" 2>/dev/null || echo "0")

    if [[ "$count" == "0" ]]; then
        log_warn "No registry credentials found in cluster pull-secret."
        rm -f "${reg_file}"
        return 0
    fi

    log_success "Found ${count} registry credential(s) from cluster pull-secret"

    # Create the Secret
    oc create secret generic "${secret_name}" \
        --from-file=registries.json="${reg_file}" \
        -n ${NAMESPACE} 2>/dev/null || {
        log_warn "Could not create registry credentials Secret."
        rm -f "${reg_file}"
        return 0
    }
    rm -f "${reg_file}"

    # Patch deployment to mount the secret
    oc patch deployment/${APP_NAME} -n ${NAMESPACE} --type=json -p '[
        {"op": "add", "path": "/spec/template/spec/volumes/-", "value": {
            "name": "registry-creds",
            "secret": {"secretName": "'"${secret_name}"'", "optional": true}
        }},
        {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts/-", "value": {
            "name": "registry-creds",
            "mountPath": "/app/secrets/registries",
            "readOnly": true
        }},
        {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {
            "name": "REGISTRIES_FILE",
            "value": "/app/secrets/registries/registries.json"
        }}
    ]' 2>/dev/null || {
        oc set volume deployment/${APP_NAME} -n ${NAMESPACE} \
            --add --name=registry-creds \
            --type=secret --secret-name="${secret_name}" \
            --mount-path=/app/secrets/registries --read-only \
            --overwrite 2>/dev/null || true
        oc set env deployment/${APP_NAME} -n ${NAMESPACE} \
            REGISTRIES_FILE=/app/secrets/registries/registries.json 2>/dev/null || true
    }

    log_success "Registry credentials seeded and mounted in deployment"
}

# Deploy to OpenShift
deploy_openshift() {
    log_info "Deploying ${APP_NAME} to OpenShift..."

    # Check prerequisites
    if ! command -v oc &> /dev/null; then
        log_error "OpenShift CLI (oc) not found. Please install it first."
        exit 1
    fi

    if ! oc whoami &> /dev/null; then
        log_error "Not logged into OpenShift. Please run 'oc login' first."
        exit 1
    fi

    log_info "Applying namespace..."
    oc apply -f openshift/namespace.yaml

    log_info "Applying RBAC..."
    oc apply -f openshift/rbac.yaml

    log_info "Applying deployment, service, and route..."
    oc apply -f openshift/deployment.yaml

    # Auto-seed registry credentials from cluster pull-secret (first deploy only)
    seed_cluster_registries

    # If mirroring, switch the deployment image to the internal registry
    if [[ "$MIRROR_MODE" == true && -n "$INTERNAL_IMAGE_REF" ]]; then
        log_info "Patching deployment to use internal image: ${INTERNAL_IMAGE_REF}"
        oc set image deployment/${APP_NAME} -n ${NAMESPACE} \
            checker="${INTERNAL_IMAGE_REF}"
        log_success "Deployment image updated to internal registry"
    fi

    # Inject proxy env vars into the deployment if --proxy was provided
    if [[ -n "$PROXY_URL" ]]; then
        local no_proxy="${NO_PROXY_HOSTS:-$ESSENTIAL_NO_PROXY}"
        log_info "Injecting proxy into deployment: ${PROXY_URL}"
        oc set env deployment/${APP_NAME} -n ${NAMESPACE} \
            HTTP_PROXY="${PROXY_URL}" \
            HTTPS_PROXY="${PROXY_URL}" \
            NO_PROXY="${no_proxy}" \
            http_proxy="${PROXY_URL}" \
            https_proxy="${PROXY_URL}" \
            no_proxy="${no_proxy}"
        log_success "Proxy environment variables injected"
    fi

    log_success "All manifests applied successfully"

    # Show the route
    ROUTE_URL=$(oc get route ${APP_NAME} -n ${NAMESPACE} -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")
    echo ""
    log_info "Application URL: https://${ROUTE_URL}"
}

# Full pipeline: build, push/mirror, deploy, restart
build_push_deploy() {
    local push_label="Push to Quay.io"
    if [[ "$MIRROR_MODE" == true ]]; then
        push_label="Mirror to internal registry"
    fi

    log_info "═══════════════════════════════════════════════════════════════"
    log_info "  Full Pipeline: Build → ${push_label} → Deploy → Restart"
    log_info "═══════════════════════════════════════════════════════════════"
    echo ""

    log_info "Step 1/4: Building image..."
    build_image
    echo ""

    if [[ "$MIRROR_MODE" == true ]]; then
        log_info "Step 2/4: Mirroring to internal registry..."
        mirror_to_internal
    else
        log_info "Step 2/4: Pushing image..."
        push_image
    fi
    echo ""

    log_info "Step 3/4: Deploying to OpenShift..."
    deploy_openshift
    echo ""

    log_info "Step 4/4: Restarting deployment..."
    restart_deployment
    echo ""

    log_success "═══════════════════════════════════════════════════════════════"
    log_success "  Pipeline complete!"
    log_success "═══════════════════════════════════════════════════════════════"

    ROUTE_URL=$(oc get route ${APP_NAME} -n ${NAMESPACE} -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")
    echo ""
    echo "  Application URL: https://${ROUTE_URL}"
    echo ""
}

# Restart deployment
restart_deployment() {
    log_info "Restarting ${APP_NAME} deployment..."
    oc rollout restart deployment/${APP_NAME} -n ${NAMESPACE}
    oc rollout status deployment/${APP_NAME} -n ${NAMESPACE}
    log_success "Deployment restarted successfully"
}

# Show OpenShift status
openshift_status() {
    log_info "OpenShift Status for ${NAMESPACE}:"
    echo ""

    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "DEPLOYMENTS"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    oc get deployments -n ${NAMESPACE} -o wide 2>/dev/null || echo "  (no deployments found)"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "PODS"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    oc get pods -n ${NAMESPACE} -o wide 2>/dev/null || echo "  (no pods found)"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "SERVICES"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    oc get services -n ${NAMESPACE} 2>/dev/null || echo "  (no services found)"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "ROUTES"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    oc get routes -n ${NAMESPACE} 2>/dev/null || echo "  (no routes found)"
    echo ""
}

# Show OpenShift logs
openshift_logs() {
    log_info "Showing ${APP_NAME} OpenShift logs (Ctrl+C to exit)..."
    oc logs -n ${NAMESPACE} deployment/${APP_NAME} --tail=100 -f
}

# Persist registry credentials to a Kubernetes Secret
persist_registries() {
    log_info "═══════════════════════════════════════════════════════════════"
    log_info "  Persist Registry Credentials to Kubernetes Secret"
    log_info "═══════════════════════════════════════════════════════════════"
    echo ""

    # Check prerequisites
    if ! command -v oc &> /dev/null; then
        log_error "OpenShift CLI (oc) not found."
        return 1
    fi
    if ! oc whoami &> /dev/null; then
        log_error "Not logged into OpenShift. Please run 'oc login' first."
        return 1
    fi

    SECRET_NAME="${APP_NAME}-registries"
    REG_FILE="/tmp/${APP_NAME}-registries.json"

    # Try to get registries.json from the running pod
    POD_NAME=$(oc get pods -n ${NAMESPACE} -l app.kubernetes.io/name=${APP_NAME} \
        --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

    if [[ -n "$POD_NAME" ]]; then
        log_info "Found running pod: ${POD_NAME}"
        log_info "Extracting registry credentials from pod..."

        if oc exec -n ${NAMESPACE} "${POD_NAME}" -- cat /app/data/registries.json > "${REG_FILE}" 2>/dev/null; then
            REG_COUNT=$(python3 -c "import json; print(len(json.load(open('${REG_FILE}'))))" 2>/dev/null || echo "0")
            if [[ "${REG_COUNT}" == "0" || "${REG_COUNT}" == "" ]]; then
                log_warn "No registry credentials found in the running pod."
                log_info "Add credentials via the web UI first, then run this command again."
                rm -f "${REG_FILE}"
                return 1
            fi
            log_success "Found ${REG_COUNT} registry credential(s)"
        else
            log_warn "Could not read registries from running pod."
            log_info "Add credentials via the web UI first, then run this command again."
            rm -f "${REG_FILE}"
            return 1
        fi
    else
        log_warn "No running pod found in namespace ${NAMESPACE}."
        echo ""
        log_info "Would you like to enter registry credentials manually?"
        read -p "Enter (y/n): " -r
        echo

        if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
            log_info "Skipping --persistent."
            return 0
        fi

        # Interactive credential entry
        REGISTRIES="[]"
        while true; do
            echo ""
            read -p "Registry host (e.g. quay.io): " -r REG_HOST
            if [[ -z "$REG_HOST" ]]; then break; fi
            read -p "Username: " -r REG_USER
            read -sp "Password/Token: " -r REG_PASS
            echo ""

            if [[ -z "$REG_USER" || -z "$REG_PASS" ]]; then
                log_warn "Username and password are required. Skipping."
                continue
            fi

            # Remove protocol prefix
            REG_HOST=$(echo "$REG_HOST" | sed 's|https\?://||' | sed 's|/$||')

            REGISTRIES=$(echo "${REGISTRIES}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
data.append({'registry': '${REG_HOST}', 'username': '${REG_USER}', 'password': '${REG_PASS}'})
print(json.dumps(data))
")
            log_success "Added ${REG_HOST}"

            read -p "Add another registry? (y/n): " -r
            if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then break; fi
        done

        REG_COUNT=$(echo "${REGISTRIES}" | python3 -c "import json, sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
        if [[ "${REG_COUNT}" == "0" ]]; then
            log_info "No credentials entered. Skipping --persistent."
            return 0
        fi

        echo "${REGISTRIES}" > "${REG_FILE}"
    fi

    # Create or update the Secret
    log_info "Creating Secret '${SECRET_NAME}' in namespace ${NAMESPACE}..."

    # Delete existing secret if it exists
    oc delete secret "${SECRET_NAME}" -n ${NAMESPACE} --ignore-not-found 2>/dev/null

    oc create secret generic "${SECRET_NAME}" \
        --from-file=registries.json="${REG_FILE}" \
        -n ${NAMESPACE}

    rm -f "${REG_FILE}"
    log_success "Secret '${SECRET_NAME}' created successfully"

    # Patch deployment to mount the secret
    log_info "Patching deployment to mount registry credentials..."

    oc patch deployment/${APP_NAME} -n ${NAMESPACE} --type=json -p '[
        {"op": "add", "path": "/spec/template/spec/volumes/-", "value": {
            "name": "registry-creds",
            "secret": {"secretName": "'"${SECRET_NAME}"'", "optional": true}
        }},
        {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts/-", "value": {
            "name": "registry-creds",
            "mountPath": "/app/secrets/registries",
            "readOnly": true
        }},
        {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {
            "name": "REGISTRIES_FILE",
            "value": "/app/secrets/registries/registries.json"
        }}
    ]' 2>/dev/null || {
        # If patch fails (e.g. volume already exists), try replacing
        log_warn "Patch failed (volume may already exist). Reapplying deployment..."
        oc set volume deployment/${APP_NAME} -n ${NAMESPACE} \
            --add --name=registry-creds \
            --type=secret --secret-name="${SECRET_NAME}" \
            --mount-path=/app/secrets/registries --read-only \
            --overwrite 2>/dev/null || true
        oc set env deployment/${APP_NAME} -n ${NAMESPACE} \
            REGISTRIES_FILE=/app/secrets/registries/registries.json 2>/dev/null || true
    }

    log_info "Restarting deployment to pick up credentials..."
    oc rollout restart deployment/${APP_NAME} -n ${NAMESPACE}
    oc rollout status deployment/${APP_NAME} -n ${NAMESPACE}

    echo ""
    log_success "═══════════════════════════════════════════════════════════════"
    log_success "  Registry credentials persisted to Secret '${SECRET_NAME}'"
    log_success "  Credentials will survive pod restarts."
    log_success "═══════════════════════════════════════════════════════════════"
    echo ""
    log_info "To update credentials: modify via web UI, then run '$0 --persistent' again."
    log_info "To remove persistence: oc delete secret ${SECRET_NAME} -n ${NAMESPACE}"
    echo ""
}

# Completely remove application from OpenShift
remove_openshift() {
    log_warn "═══════════════════════════════════════════════════════════════"
    log_warn "  COMPLETE REMOVAL FROM OPENSHIFT"
    log_warn "═══════════════════════════════════════════════════════════════"
    echo ""

    # Check prerequisites
    if ! command -v oc &> /dev/null; then
        log_error "OpenShift CLI (oc) not found."
        exit 1
    fi
    if ! oc whoami &> /dev/null; then
        log_error "Not logged into OpenShift. Please run 'oc login' first."
        exit 1
    fi

    log_warn "This will PERMANENTLY DELETE from OpenShift:"
    echo "  - Namespace:   ${NAMESPACE} (and all resources inside)"
    echo "  - RBAC:        ClusterRole, ClusterRoleBinding for ${APP_NAME}"
    echo ""
    read -p "Type 'yes' to confirm: " -r
    echo

    if [[ ! "$REPLY" == "yes" ]]; then
        log_info "Cancelled."
        exit 0
    fi

    log_info "[1/3] Removing Deployment, Service, Route..."
    oc delete -f openshift/deployment.yaml --ignore-not-found 2>/dev/null || true
    log_success "Deployment resources removed"

    log_info "[2/3] Removing RBAC resources..."
    oc delete -f openshift/rbac.yaml --ignore-not-found 2>/dev/null || true
    log_success "RBAC resources removed"

    log_info "[3/3] Removing Namespace..."
    oc delete -f openshift/namespace.yaml --ignore-not-found 2>/dev/null || true
    log_success "Namespace removed"

    echo ""
    log_success "═══════════════════════════════════════════════════════════════"
    log_success "  Application completely removed from OpenShift."
    log_success "  Reinstall with: $0 --build-push-deploy"
    log_success "═══════════════════════════════════════════════════════════════"
}

# ============================================================================
# Main Script — parse global options, then run action
# ============================================================================

ACTIONS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --proxy)
            if [[ -z "${2:-}" || "$2" == --* ]]; then
                log_error "--proxy requires a URL or 'auto' (e.g. --proxy http://proxy:8080 or --proxy auto)"
                exit 1
            fi
            if [[ "$2" == "auto" ]]; then
                if ! detect_cluster_proxy; then
                    log_error "Could not auto-detect proxy. Is 'oc' logged in? Does the cluster have a Proxy object?"
                    exit 1
                fi
            else
                PROXY_URL="$2"
            fi
            shift 2
            ;;
        --no-proxy)
            if [[ -z "${2:-}" || "$2" == --* ]]; then
                log_error "--no-proxy requires a host list (e.g. --no-proxy '.local,10.0.0.0/8')"
                exit 1
            fi
            NO_PROXY_HOSTS="$2"
            shift 2
            ;;
        --mirror)
            MIRROR_MODE=true
            shift
            ;;
        --ca-cert)
            if [[ -z "${2:-}" || "$2" == --* ]]; then
                log_error "--ca-cert requires a file path (e.g. --ca-cert /path/to/proxy-ca.pem)"
                exit 1
            fi
            if [[ ! -f "$2" ]]; then
                log_error "CA certificate file not found: $2"
                exit 1
            fi
            CUSTOM_CA_PATH="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
            log_info "Custom CA certificate: ${CUSTOM_CA_PATH}"
            shift 2
            ;;
        --help|-h)
            ACTIONS+=("help")
            shift
            ;;
        --*)
            ACTIONS+=("$1")
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Run '$0 --help' for usage information."
            exit 1
            ;;
    esac
done

if [[ -n "$PROXY_URL" ]]; then
    log_info "Proxy configured: ${PROXY_URL}"
fi

[[ ${#ACTIONS[@]} -eq 0 ]] && ACTIONS=("help")

run_action() {
    case "$1" in
        --build)            build_image ;;
        --run-local)        run_local ;;
        --stop)             stop_service ;;
        --status)           check_status ;;
        --logs)             show_logs ;;
        --destroy)          destroy_everything ;;
        --push)             push_image ;;
        --mirror-push)      mirror_to_internal ;;
        --deploy)           deploy_openshift ;;
        --build-push-deploy) build_push_deploy ;;
        --restart)          restart_deployment ;;
        --openshift-status) openshift_status ;;
        --openshift-logs)   openshift_logs ;;
        --remove)           remove_openshift ;;
        --persistent)       persist_registries ;;
        help)               show_help ;;
        *)
            log_error "Unknown action: $1"
            echo "Run '$0 --help' for usage information."
            exit 1
            ;;
    esac
}

for action in "${ACTIONS[@]}"; do
    run_action "$action" || true
done
