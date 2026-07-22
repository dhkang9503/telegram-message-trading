#!/usr/bin/env python3

import asyncio
import json
import sys

import httpx

from live_bot import LLAMA_TIMEOUT_SECONDS, LLAMA_URL, infer


async def main() -> int:
    if len(sys.argv) < 2:
        print(f'사용법: python {sys.argv[0]} "테스트할 메시지"')
        return 1

    message = " ".join(sys.argv[1:]).strip()

    if not message:
        print("오류: 빈 메시지는 테스트할 수 없습니다.")
        return 1

    try:
        async with httpx.AsyncClient(timeout=LLAMA_TIMEOUT_SECONDS) as client:
            result = await infer(client, message)
    except Exception as exc:
        print(f"추론 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"입력: {message}")
    print(f"추론 시간: {result['inference_seconds']:.3f}초")
    print("원본 출력:")
    print(result["raw_output"])
    print("파싱 결과:")
    print(
        json.dumps(
            {"actions": result["actions"]},
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
