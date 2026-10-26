from __future__ import annotations

import os
import random
import sys
import time
import uuid

import oci

SHAPE = "VM.Standard.E2.1.Micro"
INSTANCE_NAME = os.getenv("OCI_INSTANCE_NAME", "Ubuntu-E2-Micro-2")
BOOT_VOLUME_GB = int(os.getenv("OCI_BOOT_VOLUME_GB", "50"))
MIN_SLEEP = int(os.getenv("MIN_SLEEP_SECONDS", "60"))
MAX_SLEEP = int(os.getenv("MAX_SLEEP_SECONDS", "120"))
MAX_RUNTIME_MINUTES = int(os.getenv("MAX_RUNTIME_MINUTES", "0"))  # 0 = forever
MAX_E2_MICROS = int(os.getenv("MAX_E2_MICROS", "2"))

user = os.getenv("OCI_USER_OCID")
fingerprint = os.getenv("OCI_FINGERPRINT")
tenancy = os.getenv("OCI_TENANT_OCID")
region = os.getenv("OCI_REGION")
key_value = os.getenv("OCI_KEY_CONTENT") or os.getenv("OCI_KEY_FILE")

required = {
    "OCI_USER_OCID": user,
    "OCI_FINGERPRINT": fingerprint,
    "OCI_TENANT_OCID": tenancy,
    "OCI_REGION": region,
    "OCI_KEY_CONTENT/OCI_KEY_FILE": key_value,
}
missing = [name for name, value in required.items() if not value]
if missing:
    print(f"CRITICAL: missing OCI credentials/settings: {missing}")
    sys.exit(1)

config = {
    "user": user,
    "fingerprint": fingerprint,
    "tenancy": tenancy,
    "region": region,
}
if "BEGIN" in key_value:
    config["key_content"] = key_value.replace("\\n", "\n")
else:
    config["key_file"] = key_value

compartment_id = os.getenv("OCI_COMPARTMENT_ID") or tenancy
subnet_id = os.getenv("OCI_SUBNET_ID")
public_ssh_key = os.getenv("OCI_PUBLIC_SSH_KEY")
image_id_override = os.getenv("OCI_IMAGE_ID")

if not subnet_id or not public_ssh_key:
    print("CRITICAL: OCI_SUBNET_ID and OCI_PUBLIC_SSH_KEY are required")
    sys.exit(1)

if BOOT_VOLUME_GB < 50:
    print("CRITICAL: OCI_BOOT_VOLUME_GB must be at least 50 GB")
    sys.exit(1)

compute = oci.core.ComputeClient(config)
identity = oci.identity.IdentityClient(config)
network = oci.core.VirtualNetworkClient(config)


def all_instances():
    return oci.pagination.list_call_get_all_results(
        compute.list_instances,
        compartment_id=compartment_id,
    ).data


def active_e2_instances():
    active = []
    for instance in all_instances():
        if instance.lifecycle_state in ("TERMINATED", "TERMINATING"):
            continue
        if instance.shape == SHAPE:
            active.append(instance)
    return active


def existing_named_instance():
    for instance in active_e2_instances():
        if instance.display_name == INSTANCE_NAME:
            return instance
    return None


def supported_ads():
    ads = [ad.name for ad in identity.list_availability_domains(tenancy).data]
    supported = []
    for ad in ads:
        try:
            shapes = compute.list_shapes(
                compartment_id=compartment_id,
                availability_domain=ad,
                shape=SHAPE,
            ).data
            if any(shape.shape == SHAPE for shape in shapes):
                supported.append(ad)
        except Exception as exc:
            print(f"WARN: could not inspect shapes in {ad}: {exc}")
    return supported


def restrict_for_subnet(ads):
    try:
        subnet = network.get_subnet(subnet_id).data
    except Exception as exc:
        print(f"WARN: could not inspect subnet: {exc}; continuing")
        return ads

    if not subnet.availability_domain:
        print("Subnet is regional.")
        return ads

    print(f"Subnet is AD-specific: {subnet.availability_domain}")
    return [ad for ad in ads if ad == subnet.availability_domain]


def discover_image():
    if image_id_override:
        print(f"Using OCI_IMAGE_ID override: {image_id_override}")
        return image_id_override

    candidates = [
        ("Canonical Ubuntu", "24.04"),
        ("Canonical Ubuntu", "22.04"),
    ]
    for operating_system, version in candidates:
        try:
            images = compute.list_images(
                compartment_id=compartment_id,
                operating_system=operating_system,
                operating_system_version=version,
                shape=SHAPE,
                lifecycle_state="AVAILABLE",
                sort_by="TIMECREATED",
                sort_order="DESC",
            ).data
        except Exception as exc:
            print(f"WARN: image discovery failed for {operating_system} {version}: {exc}")
            continue

        if images:
            image = images[0]
            print(f"Selected image: {image.display_name} ({image.id})")
            return image.id

    raise RuntimeError(
        "No compatible Ubuntu image was discovered. Set OCI_IMAGE_ID to an x86_64 image compatible with VM.Standard.E2.1.Micro."
    )


def launch(ad, image_id):
    details = oci.core.models.LaunchInstanceDetails(
        display_name=INSTANCE_NAME,
        compartment_id=compartment_id,
        availability_domain=ad,
        shape=SHAPE,
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=image_id,
            boot_volume_size_in_gbs=BOOT_VOLUME_GB,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
            assign_private_dns_record=True,
            display_name="e2-micro-vnic",
        ),
        metadata={"ssh_authorized_keys": public_ssh_key.strip()},
    )
    return compute.launch_instance(details, opc_retry_token=str(uuid.uuid4()))


print(f"OCI region: {region}")
print(f"Target: {SHAPE} | name={INSTANCE_NAME} | boot={BOOT_VOLUME_GB} GB")

named = existing_named_instance()
if named:
    print(f"SUCCESS: target instance already exists: {named.display_name} ({named.lifecycle_state}) {named.id}")
    sys.exit(0)

current_e2 = active_e2_instances()
print(f"Active {SHAPE} instances found in compartment: {len(current_e2)}")
for instance in current_e2:
    print(f"  - {instance.display_name} | {instance.lifecycle_state} | {instance.id}")

if len(current_e2) >= MAX_E2_MICROS:
    print(f"STOP: {len(current_e2)} E2 micro instances already exist; MAX_E2_MICROS={MAX_E2_MICROS}.")
    print("This guard avoids unintentionally exceeding the intended Always Free E2-micro count.")
    sys.exit(0)

ads = restrict_for_subnet(supported_ads())
if not ads:
    print("CRITICAL: no availability domain supports VM.Standard.E2.1.Micro with the selected subnet.")
    print("Oracle documents that E2.1.Micro is available only in one AD in multi-AD regions.")
    sys.exit(1)

print(f"Eligible AD(s): {ads}")
image_id = discover_image()
start = time.monotonic()
sweep = 0

while True:
    if MAX_RUNTIME_MINUTES > 0 and (time.monotonic() - start) >= MAX_RUNTIME_MINUTES * 60:
        print(f"Reached MAX_RUNTIME_MINUTES={MAX_RUNTIME_MINUTES}; exiting cleanly.")
        sys.exit(0)

    sweep += 1
    print(f"\n=== SWEEP {sweep} ===")

    named = existing_named_instance()
    if named:
        print(f"SUCCESS: instance detected: {named.display_name} ({named.lifecycle_state}) {named.id}")
        sys.exit(0)

    current_e2 = active_e2_instances()
    if len(current_e2) >= MAX_E2_MICROS:
        print("STOP: E2 micro count reached the configured safety limit.")
        sys.exit(0)

    shuffled_ads = list(ads)
    random.shuffle(shuffled_ads)

    for ad in shuffled_ads:
        print(f"Trying {ad} ...")
        try:
            response = launch(ad, image_id)
            if response.status in (200, 201):
                instance = response.data
                print("SUCCESS: instance creation started")
                print(f"Name: {instance.display_name}")
                print(f"OCID: {instance.id}")
                print(f"AD: {ad}")
                print(f"Shape: {instance.shape}")
                sys.exit(0)

        except oci.exceptions.ServiceError as exc:
            text = f"{exc.code} {exc.message}".lower()
            if "out of host capacity" in text or "capacity" in text and exc.status >= 500:
                print(f"No host capacity in {ad}: {exc.code} - {exc.message}")
                continue
            if exc.status == 429:
                delay = random.randint(180, 300)
                print(f"Rate limited by OCI; sleeping {delay}s")
                time.sleep(delay)
                continue
            if exc.status in (401, 403):
                print(f"FATAL authentication/permission error: {exc}")
                sys.exit(1)
            if exc.status == 400:
                print(f"FATAL configuration rejected: {exc.code} - {exc.message}")
                sys.exit(1)
            if exc.status >= 500:
                print(f"OCI server/capacity error: {exc.status}/{exc.code}: {exc.message}")
                continue
            print(f"FATAL unexpected OCI API error: {exc}")
            sys.exit(1)

        except Exception as exc:
            print(f"WARN network/client exception: {exc}")
            time.sleep(15)
            named = existing_named_instance()
            if named:
                print(f"SUCCESS: instance exists despite client exception: {named.id}")
                sys.exit(0)

    delay = random.randint(MIN_SLEEP, MAX_SLEEP)
    print(f"No capacity yet. Sleeping {delay}s before next sweep...")
    time.sleep(delay)
