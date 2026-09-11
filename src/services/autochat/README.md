# Autochat Service for LunaBot

### Usage

1. Install dependencies (Python 3.8+ required):

```bash
pip install -r requirements.txt
```

2. Update the configuration of LunaBot by modifying `config/chat/autochat.yaml`.

3. Run the service:

```bash
python serve.py
```

### Historical image handling

Image URLs in stored QQ messages can expire. When Autochat needs a visual
summary, it now tries the local byte cache first, then asks the NoneBot host
to refresh the message through OneBot, and only then falls back to the stored
URL. Refreshed image bytes are cached below
`data/chat/autochat/image_cache/`; the cache is disposable and does not alter
the message or memory databases. It is bounded to 2,000 entries or 256 MiB,
whichever is reached first.

`image_caption.refresh_timeout` controls the OneBot refresh RPC timeout and
defaults to 20 seconds when omitted from an existing configuration.

