"""Probe the configured model endpoint with a one-line request.

    python -m scripts.smoke_test
"""

from feluda.llm import complete, settings

if __name__ == "__main__":
    print(settings())
    print(complete("Reply with exactly: pong", max_tokens=16))
