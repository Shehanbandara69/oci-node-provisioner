import oci
import os
import time

# Setup configuration from GitHub Secrets (Namas match karala thiyenne)
config = {
    "user": os.getenv("OCI_USER_OCID"),
    "key_content": os.getenv("OCI_KEY_FILE"),
    "fingerprint": os.getenv("OCI_FINGERPRINT"),
    "tenancy": os.getenv("OCI_TENANT_OCID"),
    "region": os.getenv("OCI_REGION")
}

try:
    # Compute client and Identity client (to auto-fetch ADs)
    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)
    print("OCI Authentication Successful. Initializing setup...")
except Exception as e:
    print(f"Authentication Failed: {e}")
    exit(1)

# Execution parameters
compartment_id = os.getenv("OCI_TENANT_OCID")
subnet_id = os.getenv("OCI_SUBNET_ID")
image_id = os.getenv("OCI_IMAGE_ID")
public_ssh_key = os.getenv("OCI_PUBLIC_SSH_KEY") 

# SAFETY CHECK: Verify the key actually loaded
if not public_ssh_key or public_ssh_key.strip() == "":
    print("CRITICAL ERROR: OCI_PUBLIC_SSH_KEY is empty or missing from your secrets!")
    exit(1)

# AUTO-FETCH Availability Domains (Me kramayen hardcode karanna ona na)
try:
    ads_response = identity_client.list_availability_domains(compartment_id)
    ads = [ad.name for ad in ads_response.data]
    print(f"Found Availability Domains for {config['region']}: {ads}")
except Exception as e:
    print(f"Failed to fetch Availability Domains: {e}")
    exit(1)

total_attempts = 60 

for i in range(1, total_attempts + 1):
    current_ad = ads[(i - 1) % len(ads)]
    print(f"[Attempt {i}/{total_attempts}] Requesting A1.Flex instance in {current_ad}...")
    
    try:
        request = oci.core.models.LaunchInstanceDetails(
            display_name="Ubuntu-ARM-Server",
            compartment_id=compartment_id,
            availability_domain=current_ad,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=4,
                memory_in_gbs=24
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                image_id=image_id,
                boot_volume_size_in_gbs=100
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
                assign_private_dns_record=True,
                display_name="arm-server-vnic"
            ),
            metadata={
                "ssh_authorized_keys": str(public_ssh_key).strip()
            }
        )
        
        response = compute_client.launch_instance(request)
        if response.status == 200:
            print("SUCCESS! Server creation initialized perfectly.")
            exit(0)
            
    except oci.exceptions.ServiceError as e:
        if "Out of host capacity" in str(e) or e.status == 500:
            print(f"-> Capacity Unavailable. Resting 60 seconds...")
        else:
            print(f"-> API Error: {e.message}")
            
    if i < total_attempts:
        time.sleep(60)
