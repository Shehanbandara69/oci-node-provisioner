import oci
import os
import time
import sys
import random
import uuid

# ============================================================
# SETTINGS
# ============================================================

INSTANCE_NAME = os.environ.get("OCI_INSTANCE_NAME", "Ubuntu-ARM-Server-2C")
SHAPE = "VM.Standard.A1.Flex"

TARGET_OCPUS = float(os.environ.get("OCI_OCPUS", "2"))
TARGET_MEMORY = float(os.environ.get("OCI_MEMORY_GB", "12"))

# If true, after some time also try 1 OCPU / 6 GB.
# Useful because smaller shapes can sometimes fit available host capacity.
ALLOW_SMALLER_FALLBACK = (
    os.environ.get("ALLOW_SMALLER_FALLBACK", "false").lower() == "true"
)

FALLBACK_AFTER_MINUTES = int(
    os.environ.get("FALLBACK_AFTER_MINUTES", "60")
)

# GitHub-hosted runner max job duration = 6 hours.
# Keep safely below that.
MAX_RUNTIME_MINUTES = int(
    os.environ.get("MAX_RUNTIME_MINUTES", "330")
)

MIN_SLEEP = 45
MAX_SLEEP = 90

# ============================================================
# OCI CONFIG
# ============================================================

user = os.environ.get("OCI_USER_OCID")
fingerprint = os.environ.get("OCI_FINGERPRINT")
tenancy = os.environ.get("OCI_TENANT_OCID")
region = os.environ.get("OCI_REGION")

# Supports either:
# OCI_KEY_CONTENT = actual PEM text
# OCI_KEY_FILE    = actual file path OR PEM text (backward compatible)
key_value = (
    os.environ.get("OCI_KEY_CONTENT")
    or os.environ.get("OCI_KEY_FILE")
)

required = {
    "OCI_USER_OCID": user,
    "OCI_FINGERPRINT": fingerprint,
    "OCI_TENANT_OCID": tenancy,
    "OCI_REGION": region,
    "OCI_KEY_CONTENT/OCI_KEY_FILE": key_value,
}

missing = [k for k, v in required.items() if not v]

if missing:
    print(f"CRITICAL ERROR: Missing secrets: {missing}")
    sys.exit(1)

config = {
    "user": user,
    "fingerprint": fingerprint,
    "tenancy": tenancy,
    "region": region,
}

# OCI SDK accepts either key_content or key_file.
if "BEGIN" in key_value:
    config["key_content"] = key_value.replace("\\n", "\n")
else:
    config["key_file"] = key_value

try:
    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)
    network_client = oci.core.VirtualNetworkClient(config)

    print("✅ OCI authentication/client initialization successful.")
except Exception as e:
    print(f"❌ Authentication/client initialization failed: {e}")
    sys.exit(1)

# ============================================================
# INSTANCE PARAMETERS
# ============================================================

compartment_id = os.environ.get("OCI_COMPARTMENT_ID") or tenancy
subnet_id = os.environ.get("OCI_SUBNET_ID")
image_id = os.environ.get("OCI_IMAGE_ID")
public_ssh_key = os.environ.get("OCI_PUBLIC_SSH_KEY")

if not subnet_id or not image_id or not public_ssh_key:
    print(
        "CRITICAL ERROR: OCI_SUBNET_ID, OCI_IMAGE_ID or "
        "OCI_PUBLIC_SSH_KEY is missing."
    )
    sys.exit(1)


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def existing_instance():
    """
    Prevent multiple GitHub Action runs from creating duplicate instances.
    """

    try:
        response = oci.pagination.list_call_get_all_results(
            compute_client.list_instances,
            compartment_id=compartment_id,
            display_name=INSTANCE_NAME
        )

        for instance in response.data:
            if instance.lifecycle_state not in (
                "TERMINATED",
                "TERMINATING"
            ):
                return instance

    except Exception as e:
        print(f"⚠️ Could not check existing instances: {e}")

    return None


instance = existing_instance()

if instance:
    print(
        f"✅ Instance already exists!\n"
        f"Name: {instance.display_name}\n"
        f"State: {instance.lifecycle_state}\n"
        f"OCID: {instance.id}"
    )
    sys.exit(0)


# ============================================================
# GET AVAILABILITY DOMAINS
# ============================================================

try:
    ad_response = identity_client.list_availability_domains(tenancy)
    ads = [ad.name for ad in ad_response.data]

    print(f"🌍 Region: {region}")
    print(f"📍 Availability Domains: {ads}")

except Exception as e:
    print(f"❌ Failed to fetch availability domains: {e}")
    sys.exit(1)

if not ads:
    print("❌ No availability domains returned.")
    sys.exit(1)


# ============================================================
# CHECK WHETHER SUBNET IS AD-SPECIFIC
# ============================================================

try:
    subnet = network_client.get_subnet(subnet_id).data

    if subnet.availability_domain:
        print(
            "⚠️ Your subnet is AD-specific: "
            f"{subnet.availability_domain}"
        )

        ads = [
            ad for ad in ads
            if ad == subnet.availability_domain
        ]

        if not ads:
            print(
                "❌ Subnet Availability Domain does not match "
                "the region AD list."
            )
            sys.exit(1)
    else:
        print("✅ Subnet is regional. All ADs can be tried.")

except Exception as e:
    print(f"⚠️ Could not inspect subnet: {e}")
    print("Continuing with discovered ADs...")


# ============================================================
# CAPACITY REPORT
# ============================================================

def check_capacity(ad, ocpus, memory):
    """
    Returns:
      True  -> OCI says capacity is available
      False -> OCI explicitly reports out of capacity
      None  -> capacity report failed; direct launch can still be attempted
    """

    try:
        shape_request = (
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=SHAPE,
                instance_shape_config=(
                    oci.core.models.CapacityReportInstanceShapeConfig(
                        ocpus=ocpus,
                        memory_in_gbs=memory
                    )
                )
            )
        )

        request = oci.core.models.CreateComputeCapacityReportDetails(
            compartment_id=tenancy,
            availability_domain=ad,
            shape_availabilities=[shape_request]
        )

        response = compute_client.create_compute_capacity_report(
            request
        )

        availabilities = response.data.shape_availabilities

        available = False

        for item in availabilities:
            status = item.availability_status
            count = item.available_count
            fault_domain = item.fault_domain or "AUTO"

            print(
                f"    Capacity: {fault_domain} | "
                f"{status} | count={count}"
            )

            if status == "AVAILABLE":
                available = True

        return available

    except oci.exceptions.ServiceError as e:
        print(
            f"    ⚠️ Capacity report unavailable: "
            f"{e.status} / {e.code} / {e.message}"
        )
        return None

    except Exception as e:
        print(f"    ⚠️ Capacity report error: {e}")
        return None


# ============================================================
# LAUNCH INSTANCE
# ============================================================

def launch_instance(ad, ocpus, memory):

    request = oci.core.models.LaunchInstanceDetails(
        display_name=INSTANCE_NAME,
        compartment_id=compartment_id,
        availability_domain=ad,

        shape=SHAPE,

        shape_config=(
            oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ocpus,
                memory_in_gbs=memory
            )
        ),

        source_details=(
            oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                image_id=image_id,
                boot_volume_size_in_gbs=100
            )
        ),

        create_vnic_details=(
            oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
                assign_private_dns_record=True,
                display_name="arm-server-vnic"
            )
        ),

        metadata={
            "ssh_authorized_keys": public_ssh_key.strip()
        }
    )

    retry_token = str(uuid.uuid4())

    return compute_client.launch_instance(
        request,
        opc_retry_token=retry_token
    )


# ============================================================
# RETRY LOOP
# ============================================================

start_time = time.monotonic()
deadline = start_time + (MAX_RUNTIME_MINUTES * 60)

sweep = 0

while time.monotonic() < deadline:

    sweep += 1

    elapsed_minutes = int(
        (time.monotonic() - start_time) / 60
    )

    print("\n" + "=" * 65)
    print(
        f"🔄 SWEEP #{sweep} | "
        f"Elapsed: {elapsed_minutes} minutes"
    )
    print("=" * 65)

    # Check again in case another workflow already succeeded.
    instance = existing_instance()

    if instance:
        print(
            f"✅ Instance detected: {instance.display_name} "
            f"({instance.lifecycle_state})"
        )
        sys.exit(0)

    # Main target is always first.
    configs = [
        (TARGET_OCPUS, TARGET_MEMORY)
    ]

    # Optional smaller target.
    if (
        ALLOW_SMALLER_FALLBACK
        and elapsed_minutes >= FALLBACK_AFTER_MINUTES
    ):
        configs.append((1.0, 6.0))

    current_ads = ads.copy()
    random.shuffle(current_ads)

    for ocpus, memory in configs:

        print(
            f"\n🎯 Checking A1.Flex "
            f"{ocpus:g} OCPU / {memory:g} GB"
        )

        for ad in current_ads:

            print(f"\n📍 Checking {ad}...")

            capacity = check_capacity(
                ad,
                ocpus,
                memory
            )

            # OCI explicitly says no capacity.
            if capacity is False:
                print("    ❌ No capacity. Skipping launch.")
                continue

            if capacity is True:
                print(
                    "    🟢 CAPACITY REPORTED AVAILABLE! "
                    "Launching immediately..."
                )
            else:
                print(
                    "    🟡 Capacity report inconclusive. "
                    "Trying direct launch..."
                )

            try:
                response = launch_instance(
                    ad,
                    ocpus,
                    memory
                )

                if response.status in (200, 201):

                    instance = response.data

                    print("\n" + "🎉" * 20)
                    print("SUCCESS! INSTANCE CREATION STARTED!")
                    print("🎉" * 20)

                    print(f"Name: {instance.display_name}")
                    print(f"OCID: {instance.id}")
                    print(f"AD: {ad}")
                    print(
                        f"Shape: {ocpus:g} OCPU / "
                        f"{memory:g} GB"
                    )

                    sys.exit(0)

            except oci.exceptions.ServiceError as e:

                error_text = (
                    f"{e.code} {e.message}"
                ).lower()

                if "out of host capacity" in error_text:
                    print(
                        "    ❌ Race lost: capacity disappeared "
                        "before launch."
                    )
                    continue

                if e.status == 429:
                    delay = random.randint(180, 300)

                    print(
                        f"    ⚠️ OCI rate limit (429). "
                        f"Sleeping {delay}s..."
                    )

                    time.sleep(delay)
                    continue

                if e.status in (401, 403):
                    print(
                        f"❌ Authentication/permission error:\n{e}"
                    )
                    sys.exit(1)

                if e.status == 400:
                    print(
                        "❌ OCI rejected the configuration."
                    )
                    print(f"Code: {e.code}")
                    print(f"Message: {e.message}")
                    sys.exit(1)

                if e.status >= 500:
                    delay = random.randint(60, 150)

                    print(
                        f"⚠️ OCI server error "
                        f"{e.status}/{e.code}: {e.message}"
                    )
                    print(f"Sleeping {delay}s...")

                    time.sleep(delay)
                    continue

                print(
                    f"❌ Unexpected OCI API error:\n"
                    f"Status: {e.status}\n"
                    f"Code: {e.code}\n"
                    f"Message: {e.message}"
                )

                sys.exit(1)

            except Exception as e:
                print(f"⚠️ Network/unknown exception: {e}")

                # Important: request might actually have succeeded.
                time.sleep(15)

                instance = existing_instance()

                if instance:
                    print(
                        "✅ Instance exists despite client-side "
                        "exception!"
                    )
                    sys.exit(0)

    delay = random.randint(
        MIN_SLEEP,
        MAX_SLEEP
    )

    print(
        f"\n😴 Sweep complete. "
        f"No capacity yet. Sleeping {delay}s..."
    )

    time.sleep(delay)


print(
    f"\n⏰ Reached runtime limit "
    f"({MAX_RUNTIME_MINUTES} minutes)."
)
print(
    "No instance was created during this run. "
    "The next scheduled workflow can continue trying."
)

sys.exit(0)
