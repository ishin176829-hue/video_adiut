from __future__ import annotations

import argparse
import getpass
import json
import os
from typing import Any

from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_ram20150501.client import Client as RamClient
from alibabacloud_ram20150501 import models as ram_models
from alibabacloud_tea_openapi.client import Client as OpenApiClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def is_already_exists(exc: Exception) -> bool:
    text = str(exc)
    return "EntityAlreadyExists" in text or "AlreadyExists" in text or "already exists" in text.lower()


def get_client_config(access_key_id: str, access_key_secret: str, endpoint: str):
    return open_api_models.Config(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        endpoint=endpoint,
    )


def get_account_id(access_key_id: str, access_key_secret: str) -> str:
    client = OpenApiClient(get_client_config(access_key_id, access_key_secret, "sts.aliyuncs.com"))
    response = client.call_api(
        open_api_models.Params(
            action="GetCallerIdentity",
            version="2015-04-01",
            protocol="HTTPS",
            pathname="/",
            method="POST",
            auth_type="AK",
            style="RPC",
            req_body_type="formData",
            body_type="json",
        ),
        open_api_models.OpenApiRequest(query={}),
        util_models.RuntimeOptions(),
    )
    account_id = str(response.get("body", {}).get("AccountId") or "").strip()
    if not account_id:
        raise RuntimeError(f"GetCallerIdentity 未返回 AccountId: {response}")
    return account_id


def ensure_role(
    client: RamClient,
    *,
    role_name: str,
    description: str,
    assume_role_policy_document: dict[str, Any],
    max_session_duration: int = 3600,
) -> None:
    document = compact_json(assume_role_policy_document)
    try:
        client.create_role(
            ram_models.CreateRoleRequest(
                role_name=role_name,
                description=description,
                assume_role_policy_document=document,
                max_session_duration=max_session_duration,
            )
        )
        print(f"created role {role_name}")
    except Exception as exc:
        if not is_already_exists(exc):
            raise
        client.update_role(
            ram_models.UpdateRoleRequest(
                role_name=role_name,
                new_description=description,
                new_assume_role_policy_document=document,
                new_max_session_duration=max_session_duration,
            )
        )
        print(f"updated role {role_name}")


def ensure_policy(client: RamClient, *, policy_name: str, description: str, policy_document: dict[str, Any]) -> None:
    document = compact_json(policy_document)
    try:
        client.create_policy(
            ram_models.CreatePolicyRequest(
                policy_name=policy_name,
                description=description,
                policy_document=document,
            )
        )
        print(f"created policy {policy_name}")
    except Exception as exc:
        if not is_already_exists(exc):
            raise
        client.create_policy_version(
            ram_models.CreatePolicyVersionRequest(
                policy_name=policy_name,
                policy_document=document,
                rotate_strategy="DeleteOldestNonDefaultVersionWhenLimitExceeded",
                set_as_default=True,
            )
        )
        print(f"updated policy {policy_name}")


def attach_policy_to_role(client: RamClient, *, policy_name: str, role_name: str) -> None:
    try:
        client.attach_policy_to_role(
            ram_models.AttachPolicyToRoleRequest(
                policy_type="Custom",
                policy_name=policy_name,
                role_name=role_name,
            )
        )
        print(f"attached policy {policy_name} to role {role_name}")
    except Exception as exc:
        text = str(exc)
        if "EntityAlreadyExists" in text or "AlreadyExists" in text or "already attached" in text.lower():
            print(f"policy {policy_name} already attached to role {role_name}")
            return
        raise


def attach_role_to_instance(client: EcsClient, *, region_id: str, instance_id: str, role_name: str) -> None:
    try:
        client.attach_instance_ram_role(
            ecs_models.AttachInstanceRamRoleRequest(
                region_id=region_id,
                ram_role_name=role_name,
                instance_ids=json.dumps([instance_id]),
            )
        )
        print(f"attached role {role_name} to instance {instance_id}")
    except Exception as exc:
        text = str(exc)
        if "InvalidInstanceRamRole.NotSupport" in text or "already" in text.lower():
            print(f"instance {instance_id} already has role {role_name} or does not require update")
            return
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision minimal Aliyun RAM roles for SN2S OSS STS upload.")
    parser.add_argument("--region-id", default="cn-hangzhou")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--bucket", default="hd-audit-oss")
    parser.add_argument("--prefix", default="sn2s-video-audit/prod")
    parser.add_argument("--signer-role-name", default="sn2s-video-audit-ecs-signer")
    parser.add_argument("--upload-role-name", default="sn2s-video-audit-upload")
    args = parser.parse_args()

    access_key_id = os.getenv("ALIYUN_ACCESS_KEY_ID") or input("ALIYUN_ACCESS_KEY_ID: ").strip()
    access_key_secret = os.getenv("ALIYUN_ACCESS_KEY_SECRET") or getpass.getpass("ALIYUN_ACCESS_KEY_SECRET: ").strip()
    if not access_key_id or not access_key_secret:
        raise SystemExit("missing Aliyun access key")

    account_id = get_account_id(access_key_id, access_key_secret)
    ram = RamClient(get_client_config(access_key_id, access_key_secret, "ram.aliyuncs.com"))
    ecs = EcsClient(get_client_config(access_key_id, access_key_secret, "ecs-cn-hangzhou.aliyuncs.com"))

    signer_role_arn = f"acs:ram::{account_id}:role/{args.signer_role_name}"
    upload_role_arn = f"acs:ram::{account_id}:role/{args.upload_role_name}"
    object_prefix = args.prefix.strip("/") + "/*"
    bucket_resource = f"acs:oss:*:*:{args.bucket}"
    object_resource = f"acs:oss:*:*:{args.bucket}/{object_prefix}"

    ensure_role(
        ram,
        role_name=args.signer_role_name,
        description="SN2S video audit ECS signer role for OSS/ST​S upload chain",
        assume_role_policy_document={
            "Version": "1",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": ["ecs.aliyuncs.com"]},
                }
            ],
        },
    )
    ensure_role(
        ram,
        role_name=args.upload_role_name,
        description="SN2S video audit browser upload role assumed by ECS signer",
        assume_role_policy_document={
            "Version": "1",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"RAM": [signer_role_arn]},
                }
            ],
        },
    )

    ensure_policy(
        ram,
        policy_name="sn2s-video-audit-ecs-signer-policy",
        description="Allow SN2S signer ECS role to read OSS objects and assume upload role",
        policy_document={
            "Version": "1",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["sts:AssumeRole"],
                    "Resource": [upload_role_arn],
                },
                {
                    "Effect": "Allow",
                    "Action": ["oss:GetObject", "oss:GetObjectMeta", "oss:ListObjects"],
                    "Resource": [bucket_resource, object_resource],
                },
            ],
        },
    )
    ensure_policy(
        ram,
        policy_name="sn2s-video-audit-upload-policy",
        description="Allow SN2S browser STS sessions to upload only to the audit prefix",
        policy_document={
            "Version": "1",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "oss:PutObject",
                        "oss:InitiateMultipartUpload",
                        "oss:UploadPart",
                        "oss:CompleteMultipartUpload",
                        "oss:AbortMultipartUpload",
                        "oss:ListParts",
                    ],
                    "Resource": [object_resource],
                }
            ],
        },
    )
    attach_policy_to_role(ram, policy_name="sn2s-video-audit-ecs-signer-policy", role_name=args.signer_role_name)
    attach_policy_to_role(ram, policy_name="sn2s-video-audit-upload-policy", role_name=args.upload_role_name)
    attach_role_to_instance(ecs, region_id=args.region_id, instance_id=args.instance_id, role_name=args.signer_role_name)

    print(json.dumps({
        "account_id": account_id,
        "signer_role_arn": signer_role_arn,
        "upload_role_arn": upload_role_arn,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
