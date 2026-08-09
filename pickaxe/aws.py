"""Thin boto3 helpers. Everything that talks to AWS lives here."""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .config import Config

UBUNTU_SSM_PARAM = "/aws/service/canonical/ubuntu/server/24.04/stable/current/{arch}/hvm/ebs-gp3/ami-id"


class AwsError(Exception):
    pass


@dataclass
class Stack:
    name: str
    status: str
    outputs: dict[str, str]

    @property
    def instance_id(self) -> str:
        value = self.outputs.get("PickaxeInstanceId")
        if not value:
            raise AwsError(f"stack {self.name} has no instance yet")
        return value

    @property
    def public_ip(self) -> str | None:
        return self.outputs.get("PickaxePublicIp")


def session(cfg: Config) -> boto3.Session:
    return boto3.Session(profile_name=cfg.aws.profile, region_name=cfg.aws.region)


def account_id(sess: boto3.Session) -> str:
    return sess.client("sts").get_caller_identity()["Account"]


# --------------------------------------------------------------------------- AMI


def instance_arch(instance_type: str) -> str:
    """Return 'arm64' or 'amd64' for an EC2 instance type.

    Graviton families carry a 'g' in the letters that follow the generation
    digit: t4g, m6gd, c7gn, r8g, x2gd, g5g. Intel/AMD families do not (c5n,
    m5dn, g4dn).
    """
    family = instance_type.split(".")[0]
    match = re.match(r"^[a-z]+[0-9]+(?P<suffix>[a-z]*)$", family)
    if match and "g" in match.group("suffix"):
        return "arm64"
    return "amd64"


def resolve_ami(sess: boto3.Session, arch: str) -> str:
    param = UBUNTU_SSM_PARAM.format(arch=arch)
    try:
        return sess.client("ssm").get_parameter(Name=param)["Parameter"]["Value"]
    except ClientError as exc:
        raise AwsError(
            f"could not resolve an Ubuntu 24.04 {arch} AMI in {sess.region_name} "
            f"(SSM parameter {param}): {exc}"
        ) from exc


def root_device_name(sess: boto3.Session, ami_id: str) -> str:
    images = sess.client("ec2").describe_images(ImageIds=[ami_id])["Images"]
    if not images:
        raise AwsError(f"AMI {ami_id} not found in {sess.region_name}")
    return images[0].get("RootDeviceName", "/dev/sda1")


# --------------------------------------------------------------------------- S3


def ensure_bucket(sess: boto3.Session, name: str) -> None:
    s3 = sess.client("s3")
    region = sess.region_name
    try:
        s3.head_bucket(Bucket=name)
        _set_lifecycle(s3, name)  # also covers buckets made by older versions
        return
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "403":
            raise AwsError(
                f"S3 bucket {name} exists but belongs to another account. "
                "Pick a different aws.s3_bucket in config.yaml."
            ) from exc
        if code not in ("404", "NoSuchBucket"):
            raise

    kwargs = {"Bucket": name}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    s3.get_waiter("bucket_exists").wait(Bucket=name)
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]
        },
    )
    _set_lifecycle(s3, name)


def _set_lifecycle(s3, name: str) -> None:
    """Stop abandoned multipart uploads from billing forever.

    A cancelled world upload leaves its uploaded parts behind, and S3 charges
    for them until they are aborted -- with nothing in the console's object
    list to hint at why.
    """
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "pickaxe-abort-incomplete-uploads",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3},
                }
            ]
        },
    )


def abort_stale_uploads(sess: boto3.Session, bucket: str, key: str) -> int:
    """Abort any half-finished multipart upload for `key`. Returns how many."""
    s3 = sess.client("s3")
    try:
        pending = s3.list_multipart_uploads(Bucket=bucket, Prefix=key).get(
            "Uploads", []
        )
    except ClientError:
        return 0
    for upload_job in pending:
        s3.abort_multipart_upload(
            Bucket=bucket, Key=upload_job["Key"], UploadId=upload_job["UploadId"]
        )
    return len(pending)


def upload(sess: boto3.Session, bucket: str, key: str, data: bytes) -> None:
    sess.client("s3").put_object(Bucket=bucket, Key=key, Body=data)


PART_SIZE = 16 * 1_048_576

# boto3's defaults give up quickly and, worse, abort the whole multipart upload
# on failure. On a slow or lossy link that throws away hours of transfer.
_TRANSFER_CONFIG = BotoConfig(
    retries={"max_attempts": 10, "mode": "adaptive"},
    connect_timeout=30,
    read_timeout=120,
)


def _find_upload(s3, bucket: str, key: str) -> str | None:
    uploads = [
        u
        for u in s3.list_multipart_uploads(Bucket=bucket, Prefix=key).get("Uploads", [])
        if u["Key"] == key
    ]
    if not uploads:
        return None
    uploads.sort(key=lambda u: u["Initiated"], reverse=True)
    return uploads[0]["UploadId"]


def _list_parts(
    s3, bucket: str, key: str, upload_id: str
) -> dict[int, tuple[str, int]]:
    parts: dict[int, tuple[str, int]] = {}
    marker = 0
    while True:
        page = s3.list_parts(
            Bucket=bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker
        )
        for part in page.get("Parts", []):
            parts[part["PartNumber"]] = (part["ETag"].strip('"'), part["Size"])
        if not page.get("IsTruncated"):
            return parts
        marker = page["NextPartNumberMarker"]


def _verify_parts(
    path: Path, parts: dict[int, tuple[str, int]], part_size: int
) -> dict[int, str]:
    """Keep only parts whose S3 checksum matches the local file.

    A part's ETag is the MD5 of its bytes, so this proves the remote part came
    from exactly this archive. Anything that fails is simply re-uploaded.
    """
    verified: dict[int, str] = {}
    total = path.stat().st_size
    with path.open("rb") as handle:
        for number, (etag, size) in sorted(parts.items()):
            offset = (number - 1) * part_size
            if offset + size > total:
                continue
            handle.seek(offset)
            if hashlib.md5(handle.read(size)).hexdigest() == etag:
                verified[number] = etag
    return verified


def upload_file(
    sess: boto3.Session,
    bucket: str,
    key: str,
    path,
    on_progress=None,
    on_resume=None,
) -> None:
    """Resumable multipart upload.

    Parts already in S3 from an interrupted run are verified against the local
    file and reused, so a dropped connection costs one part, not the transfer.
    Nothing is aborted on failure -- the parts are what makes the retry cheap.
    """
    s3 = sess.client("s3", config=_TRANSFER_CONFIG)
    path = Path(path)
    total = path.stat().st_size

    part_size = PART_SIZE
    done: dict[int, str] = {}
    upload_id = _find_upload(s3, bucket, key)

    if upload_id:
        existing = _list_parts(s3, bucket, key, upload_id)
        if existing:
            # Every part but the last is full size, so the largest is the size
            # the interrupted run used. Resuming requires matching it.
            part_size = max(size for _, size in existing.values())
            done = _verify_parts(path, existing, part_size)
        if not done:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            upload_id, part_size = None, PART_SIZE

    if upload_id is None:
        upload_id = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]

    count = max(1, math.ceil(total / part_size))
    if done and on_resume:
        on_resume(
            len(done),
            count,
            sum(min(part_size, total - (n - 1) * part_size) for n in done),
        )

    completed = [
        {"PartNumber": n, "ETag": f'"{tag}"'} for n, tag in sorted(done.items())
    ]
    with path.open("rb") as handle:
        for number in range(1, count + 1):
            offset = (number - 1) * part_size
            length = min(part_size, total - offset)
            if number in done:
                if on_progress:
                    on_progress(length)
                continue
            handle.seek(offset)
            body = handle.read(length)
            etag = _upload_part(s3, bucket, key, upload_id, number, body)
            completed.append({"PartNumber": number, "ETag": etag})
            if on_progress:
                on_progress(length)

    completed.sort(key=lambda p: p["PartNumber"])
    s3.complete_multipart_upload(
        Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": completed}
    )


def _upload_part(
    s3, bucket: str, key: str, upload_id: str, number: int, body: bytes
) -> str:
    last: Exception | None = None
    for attempt in range(5):
        try:
            return s3.upload_part(
                Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=body
            )["ETag"]
        except (BotoCoreError, ClientError) as exc:
            last = exc
            time.sleep(min(2**attempt, 15))
    raise AwsError(
        f"part {number} failed after 5 attempts ({last}). "
        "Re-run `pickaxe up` -- finished parts are kept and will be skipped."
    )


def get_object(sess: boto3.Session, bucket: str, key: str) -> bytes | None:
    try:
        return sess.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def list_backups(sess: boto3.Session, bucket: str) -> list[dict]:
    paginator = sess.client("s3").get_paginator("list_objects_v2")
    items: list[dict] = []
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix="backups/"):
            items.extend(page.get("Contents", []))
    except ClientError as exc:
        # A bucket that does not exist yet simply has no backups in it.
        if exc.response["Error"]["Code"] in ("NoSuchBucket", "404"):
            return []
        raise
    items.sort(key=lambda o: o["LastModified"], reverse=True)
    return items


def empty_bucket(sess: boto3.Session, bucket: str) -> None:
    s3 = sess.client("s3")
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        keys = [
            {"Key": o["Key"], "VersionId": o["VersionId"]}
            for o in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})


# --------------------------------------------------------------------- CloudFormation


def get_stack(sess: boto3.Session, name: str) -> Stack | None:
    try:
        stacks = sess.client("cloudformation").describe_stacks(StackName=name)["Stacks"]
    except ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise
    stack = stacks[0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    return Stack(name=name, status=stack["StackStatus"], outputs=outputs)


def require_stack(sess: boto3.Session, name: str) -> Stack:
    stack = get_stack(sess, name)
    if stack is None:
        raise AwsError(f"server {name!r} is not deployed yet. Run `pickaxe up` first.")
    return stack


# --------------------------------------------------------------------------- EC2


def ensure_default_vpc(sess: boto3.Session) -> None:
    """The stack launches into the default VPC; some accounts have none."""
    vpcs = sess.client("ec2").describe_vpcs(
        Filters=[{"Name": "is-default", "Values": ["true"]}]
    )["Vpcs"]
    if not vpcs:
        raise AwsError(
            f"no default VPC in {sess.region_name}, and Pickaxe launches into it.\n"
            "  Create one with: aws ec2 create-default-vpc --region "
            f"{sess.region_name}\n"
            "  (or pick a region that still has its default VPC)"
        )


def instance_details(sess: boto3.Session, instance_id: str) -> dict:
    """Facts about the live instance, used to keep re-deploys non-destructive."""
    ec2 = sess.client("ec2")
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    if not reservations or not reservations[0]["Instances"]:
        raise AwsError(f"instance {instance_id} not found")
    inst = reservations[0]["Instances"][0]

    root_device = inst.get("RootDeviceName", "/dev/sda1")
    root_size = None
    for mapping in inst.get("BlockDeviceMappings", []):
        if mapping["DeviceName"] == root_device:
            volume_id = mapping["Ebs"]["VolumeId"]
            volumes = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"]
            if volumes:
                root_size = volumes[0]["Size"]
            break

    return {
        "state": inst["State"]["Name"],
        "instance_type": inst["InstanceType"],
        "image_id": inst["ImageId"],
        "root_device": root_device,
        "root_size": root_size,
        "public_ip": inst.get("PublicIpAddress"),
        "launch_time": inst.get("LaunchTime"),
    }


def instance_state(sess: boto3.Session, instance_id: str) -> str:
    reservations = sess.client("ec2").describe_instances(InstanceIds=[instance_id])[
        "Reservations"
    ]
    if not reservations or not reservations[0]["Instances"]:
        raise AwsError(f"instance {instance_id} not found")
    return reservations[0]["Instances"][0]["State"]["Name"]


def start_instance(sess: boto3.Session, instance_id: str) -> None:
    sess.client("ec2").start_instances(InstanceIds=[instance_id])


def stop_instance(sess: boto3.Session, instance_id: str) -> None:
    sess.client("ec2").stop_instances(InstanceIds=[instance_id])


def wait_for_state(
    sess: boto3.Session, instance_id: str, target: str, timeout: int = 300
) -> None:
    waiter_name = {"running": "instance_running", "stopped": "instance_stopped"}[target]
    waiter = sess.client("ec2").get_waiter(waiter_name)
    waiter.wait(
        InstanceIds=[instance_id],
        WaiterConfig={"Delay": 5, "MaxAttempts": max(1, timeout // 5)},
    )


# --------------------------------------------------------------------------- SSM


def wait_for_ssm(sess: boto3.Session, instance_id: str, timeout: int = 300) -> None:
    """Block until the SSM agent on the instance is reachable."""
    ssm = sess.client("ssm")
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )["InstanceInformationList"]
        if info and info[0].get("PingStatus") == "Online":
            return
        time.sleep(5)
    raise AwsError(
        f"instance {instance_id} did not register with SSM within {timeout}s. "
        "It may still be booting -- try again in a minute."
    )


def run_shell(
    sess: boto3.Session,
    instance_id: str,
    commands: list[str],
    timeout: int = 900,
    comment: str = "pickaxe",
) -> str:
    """Run a shell snippet on the instance via SSM and return combined output."""
    ssm = sess.client("ssm")
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment=comment[:100],
        Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
    )["Command"]["CommandId"]

    deadline = time.time() + timeout + 30
    while time.time() < deadline:
        time.sleep(3)
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvocationDoesNotExist":
                continue
            raise
        status = result["Status"]
        if status in ("Pending", "InProgress", "Delayed"):
            continue
        out = (result.get("StandardOutputContent") or "").rstrip()
        err = (result.get("StandardErrorContent") or "").rstrip()
        if status != "Success":
            detail = "\n".join(part for part in (out, err) if part)
            raise AwsError(
                f"remote command {status.lower()} on {instance_id}:\n{detail}"
            )
        return "\n".join(part for part in (out, err) if part)

    raise AwsError(f"remote command timed out after {timeout}s")
