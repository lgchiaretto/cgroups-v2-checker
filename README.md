# cgroups v2 Checker

OpenShift 4.19 cgroups v2 Compatibility Scanner

| [Documentacao em Portugues](README.pt-br.md) | [English Documentation](README.en.md) |
|---|---|

---

Web application that scans OpenShift clusters to identify container images that may have cgroups v2 compatibility issues before upgrading to OpenShift 4.19 (RHCOS 9, cgroups v2 mandatory).

Uses **skopeo** for remote image metadata inspection without downloading layers.
