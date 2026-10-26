# Drop-in replacement for the existing A1 GitHub Actions provisioner

This version reuses the existing GitHub secrets:
- OCI_USER_OCID
- OCI_KEY_FILE
- OCI_FINGERPRINT
- OCI_TENANT_OCID
- OCI_REGION
- OCI_SUBNET_ID
- OCI_PUBLIC_SSH_KEY
- OCI_COMPARTMENT_ID (optional)

Do NOT pass the old OCI_IMAGE_ID secret: the old value may point to an ARM/A1 image. The E2 script automatically discovers a compatible x86_64 Ubuntu image.

Replace the old repository files with these files. In particular, overwrite `.github/workflows/oci_spawn.yml` so the old A1 workflow stops running and the E2 Micro workflow runs instead.
