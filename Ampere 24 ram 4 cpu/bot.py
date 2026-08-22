import oci
import os
import time
import sys

# Setup configuration from OS Environment Variables
config = {
    "user": os.environ.get("OCI_USER_OCID"),
    "key_content": os.environ.get("OCI_KEY_FILE"),
    "fingerprint": os.environ.get("OCI_FINGERPRINT"),
    "tenancy": os.environ.get("OCI_TENANT_OCID"),
    "region": os.environ.get("OCI_REGION")
}

# 1. SAFETY CHECK: Ensure keys are loaded correctly
missing_keys = [k for k, v in config.items() if not v]
if missing_keys:
    print(f"CRITICAL ERROR: These secrets are missing or not passed to Python: {missing_keys}")
    print("Please check your .github/workflows/oci_spawn.yml file's 'env:' section.")
    sys.exit(1)

try:
    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)
    print("OCI Authentication Successful. Initializing setup...")
except Exception as e:
    print(f"Authentication Failed: {e}")
    sys.exit(1)

# Execution parameters
compartment_id = os.environ.get("OCI_TENANT_OCID")
subnet_id = os.environ.get("OCI_SUBNET_ID")
image_id = os.environ.get("OCI_IMAGE_ID")
public_ssh_key = os.environ.get("OCI_PUBLIC_SSH_KEY") 

# 2. SAFETY CHECK: Ensure instance parameters are loaded
if not public_ssh_key or not subnet_id or not image_id:
    print("CRITICAL ERROR: Instance configuration secrets (Subnet, Image, SSH Key) are missing.")
    sys.exit(1)

# AUTO-FETCH Availability Domains for your region
try:
    ads_response = identity_client.list_availability_domains(compartment_id)
    ads = [ad.name for ad in ads_response.data]
    print(f"Found Availability Domains for {config['region']}: {ads}")
except Exception as e:
    print(f"Failed to fetch Availability Domains: {e}")
    sys.exit(1)

total_attempts = 100 

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
            sys.exit(0)
            
    except oci.exceptions.ServiceError as e:
        if "Out of host capacity" in str(e) or e.status == 500:
            print(f"-> Capacity Unavailable. Resting 120 seconds...")
            time.sleep(120)  
        elif e.status == 429: # 429 is Too Many Requests
            print(f"-> API Error: Too many requests. Resting 180 seconds to avoid block...")
            time.sleep(180)
        else:
            print(f"-> API Error: {e.message}")
            time.sleep(180)
