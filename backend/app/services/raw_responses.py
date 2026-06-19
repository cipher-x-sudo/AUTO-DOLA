from __future__ import annotations


RAW_RESPONSE_CHUNK_SIZE = 3500


def split_response_body(body: str, chunk_size: int = RAW_RESPONSE_CHUNK_SIZE) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if body == "":
        return [""]
    return [body[index : index + chunk_size] for index in range(0, len(body), chunk_size)]


def format_raw_response_logs(
    response_type: str,
    attempt: int,
    status_code: int,
    body: str,
    *,
    chunk_size: int = RAW_RESPONSE_CHUNK_SIZE,
) -> list[str]:
    chunks = split_response_body(body, chunk_size)
    byte_length = len(body.encode("utf-8"))
    return [
        (
            f"RAW {response_type} response {index}/{len(chunks)} "
            f"(attempt={attempt}, status={status_code}, bytes={byte_length})\n{chunk}"
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
