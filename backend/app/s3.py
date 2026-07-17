import boto3

from app.config import settings

_client = boto3.client(
    "s3",
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
)


def _full_key(key: str) -> str:
    prefix = settings.s3_prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


def put_image(key: str, body: bytes, content_type: str) -> str:
    full_key = _full_key(key)
    _client.put_object(
        Bucket=settings.s3_bucket,
        Key=full_key,
        Body=body,
        ContentType=content_type,
    )
    return full_key


def presigned_get_url(s3_key: str) -> str:
    return _client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": s3_key},
        ExpiresIn=settings.presigned_url_ttl_seconds,
    )
