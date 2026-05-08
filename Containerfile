# It's safe to use the latest and greatest here
# hadolint ignore=DL3007
FROM registry.access.redhat.com/ubi9/python-312:latest

# Setting to SemVer with no breaking changes in between
ARG SKOPEO_VER="2:1.20.*"

# Optional: corporate CA certificate for environments with TLS-intercepting
# proxies. Set via --build-arg CUSTOM_CA=<filename> and place the PEM file
# in the build context root.
ARG CUSTOM_CA=""

LABEL name="cgroups-v2-checker" \
      summary="OpenShift cgroups v2 Compatibility Checker" \
      description="Web application that scans OpenShift clusters for container images with cgroups v2 compatibility issues before upgrading to OCP 4.19." \
      io.k8s.display-name="cgroups v2 Checker" \
      io.openshift.tags="openshift,cgroups,scanner"

# Better stick to 0 instead of 'root'
USER 0

# If a custom CA was provided, install it into the system trust store
# so dnf, pip, skopeo, and curl all trust the proxy's TLS certificate.
COPY ${CUSTOM_CA:-.containerignore} /tmp/_custom_ca
RUN if [ "$CUSTOM_CA" != "" ] && [ -f /tmp/_custom_ca ]; then \
      cp /tmp/_custom_ca /etc/pki/ca-trust/source/anchors/ && \
      update-ca-trust; \
    fi && rm -f /tmp/_custom_ca

# Install skopeo (required for remote image inspection)
# Ignoring due to known issue https://github.com/hadolint/hadolint/issues/1136 with Hadolint v2.14.0
# hadolint ignore=DL3041
RUN dnf install --nodocs --assumeyes \
      "skopeo-${SKOPEO_VER}" && \
    dnf clean all && \
    rm -rf /var/cache/dnf

# Create app directories
RUN mkdir -p /app/data/reports && \
    chown -R 1001:0 /app && \
    chmod -R g=u /app

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    find . -regex '^.*\(__pycache__\|\.py[co]\)$' -delete

# Copy application code
COPY gunicorn.conf.py .
COPY run.py .
COPY app/ app/

USER 1001

EXPOSE 8080

CMD ["gunicorn", "-c", "gunicorn.conf.py", "run:app"]
