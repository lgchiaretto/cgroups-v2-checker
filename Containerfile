FROM registry.access.redhat.com/ubi9/python-311:latest

LABEL name="cgroups-v2-checker" \
      summary="OpenShift cgroups v2 Compatibility Checker" \
      description="Web application that scans OpenShift clusters for container images with cgroups v2 compatibility issues before upgrading to OCP 4.19." \
      io.k8s.display-name="cgroups v2 Checker" \
      io.openshift.tags="openshift,cgroups,scanner"

USER root

# Install skopeo (required for remote image inspection)
RUN dnf install -y --nodocs skopeo && \
    dnf clean all && \
    rm -rf /var/cache/dnf

# Create app directories
RUN mkdir -p /app/data/reports && \
    chown -R 1001:0 /app && \
    chmod -R g=u /app

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY gunicorn.conf.py .
COPY run.py .
COPY app/ app/

USER 1001

EXPOSE 8080

CMD ["gunicorn", "-c", "gunicorn.conf.py", "run:app"]
