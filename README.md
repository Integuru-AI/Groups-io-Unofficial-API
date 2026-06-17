# Groups.io Unofficial API

Unofficial Python integrations for Groups.io.

## Integrations

- `groups_io_download_message_attachment.py` - `download_message_attachment`.
- `groups_io_get_topic_messages.py` - `get_topic_messages`.
- `groups_io_search_groups.py` - `search_groups`.

## Usage

Each file exposes a `run(input, context)` or `run(headers, input)` style entrypoint, matching the source integration runtime.
Authenticated request headers/cookies are expected to be supplied by the caller when required.

Install dependencies:

```bash
pip install -r requirements.txt
```

## Info

This unofficial API is built by [Integuru](https://integuru.com).

For custom requests or hosted authentication, contact richard@integuru.com or [schedule time with us](https://calendly.com/d/cqb8-d9x-nbf/integuru).

See the [complete list of APIs by Integuru](https://github.com/Integuru-AI/APIs-by-Integuru).
