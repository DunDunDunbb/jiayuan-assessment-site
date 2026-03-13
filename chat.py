import os
import sys

from dotenv import load_dotenv
from volcenginesdkarkruntime import Ark


def main() -> None:
    load_dotenv()

    api_key = os.getenv("ARK_API_KEY")
    base_url = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    model = os.getenv("ARK_MODEL")

    if not api_key:
        raise SystemExit("Missing ARK_API_KEY in .env")

    if not model or model == "ep-your-endpoint-id":
        raise SystemExit(
            "Set ARK_MODEL in .env to your Volcengine Endpoint ID, for example ep-xxxxxxxx."
        )

    prompt = " ".join(sys.argv[1:]).strip() or "你好，介绍一下你自己。"

    client = Ark(api_key=api_key, base_url=base_url)
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )

    print(completion.choices[0].message.content)


if __name__ == "__main__":
    main()

