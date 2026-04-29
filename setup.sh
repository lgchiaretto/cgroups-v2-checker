#!/bin/bash
#
# cgroups v2 Checker - Setup Script
#
# This script helps you build, push, and deploy the cgroups-v2-checker
# application using Podman and OpenShift CLI.
#
# Usage:
#   ./setup.sh [options]
#
# Options:
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
#     --deploy               Deploy to OpenShift (apply manifests)
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
    head -35 "$0" | tail -33
    exit 0
}

# ============================================================================
# Local Development Functions
# ============================================================================

# Build container image
build_image() {
    log_info "Building ${APP_NAME} container image..."
    podman build -t "${IMAGE_REF}" -f Containerfile .
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
    podman push "${IMAGE_REF}"
    log_success "Image pushed: ${IMAGE_REF}"
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

    log_success "All manifests applied successfully"

    # Show the route
    ROUTE_URL=$(oc get route ${APP_NAME} -n ${NAMESPACE} -o jsonpath='{.spec.host}' 2>/dev/null || echo "pending")
    echo ""
    log_info "Application URL: https://${ROUTE_URL}"
}

# Full pipeline: build, push, deploy, restart
build_push_deploy() {
    log_info "═══════════════════════════════════════════════════════════════"
    log_info "  Full Pipeline: Build → Push → Deploy → Restart"
    log_info "═══════════════════════════════════════════════════════════════"
    echo ""

    log_info "Step 1/4: Building image..."
    build_image
    echo ""

    log_info "Step 2/4: Pushing image..."
    push_image
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
        exit 1
    fi
    if ! oc whoami &> /dev/null; then
        log_error "Not logged into OpenShift. Please run 'oc login' first."
        exit 1
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
                exit 1
            fi
            log_success "Found ${REG_COUNT} registry credential(s)"
        else
            log_warn "Could not read registries from running pod."
            log_info "Add credentials via the web UI first, then run this command again."
            rm -f "${REG_FILE}"
            exit 1
        fi
    else
        log_warn "No running pod found in namespace ${NAMESPACE}."
        echo ""
        log_info "Would you like to enter registry credentials manually?"
        read -p "Enter (y/n): " -r
        echo

        if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
            log_info "Cancelled."
            exit 0
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
            log_info "No credentials entered. Cancelled."
            exit 0
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
# Main Script
# ============================================================================

case "${1:-}" in
    --build)
        build_image
        ;;
    --run-local)
        run_local
        ;;
    --stop)
        stop_service
        ;;
    --status)
        check_status
        ;;
    --logs)
        show_logs
        ;;
    --destroy)
        destroy_everything
        ;;
    --push)
        push_image
        ;;
    --deploy)
        deploy_openshift
        ;;
    --build-push-deploy)
        build_push_deploy
        ;;
    --restart)
        restart_deployment
        ;;
    --openshift-status)
        openshift_status
        ;;
    --openshift-logs)
        openshift_logs
        ;;
    --remove)
        remove_openshift
        ;;
    --persistent)
        persist_registries
        ;;
    --help|-h|"")
        show_help
        ;;
    *)
        log_error "Unknown option: $1"
        echo "Run '$0 --help' for usage information."
        exit 1
        ;;
esac
