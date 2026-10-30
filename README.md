# Alexa Toby AI

A self-hosted backend that lets an Amazon Alexa custom skill answer questions with a Gemini-hosted Gemma model, so the Echo's voice and speakers stay stock.

You cannot replace the conversation agent inside an Echo, because the speech recognition and language understanding run in Amazon's cloud. A custom skill is the supported way to substitute the brain while keeping the wake word, Drop In and Announcements intact.

## How it works

```
Echo Dot  --voice-->  Amazon Alexa  --HTTPS POST-->  your endpoint  --HTTPS-->  Gemini API
                            ^                              |
                            +------- spoken reply ---------+
```

- The skill receives transcribed text only, never raw audio.
- The reply must arrive within 8 seconds.
- The endpoint must be public HTTPS with a CA-trusted certificate. A Cloudflare Tunnel is the documented approach.

## Requirements

- Python 3.12 or newer (standard library only, nothing to install)
- A Google Gemini API key
- An Alexa developer account
- A public HTTPS endpoint pointing at the service

## Quick start

1. Clone the repo.

   ```sh
   git clone https://github.com/ayaanngandhi/alexa-toby.git
   cd alexa-toby
   ```

2. Copy `.env.example` to `~/.env` and put the Gemini key in it.

   ```sh
   cp .env.example ~/.env
   $EDITOR ~/.env
   ```

3. Run it in the foreground to test:

   ```sh
   PORT=8432 python3 alexa_toby.py
   ```

4. Verify the health endpoint. This should return `{"status": "ok"}`:

   ```sh
   curl -s localhost:8432/health
   ```

5. Install the systemd user unit:

   ```sh
   cp alexa-toby.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now alexa-toby.service
   ```

   To keep the service running after you log out, enable lingering:

   ```sh
   loginctl enable-linger $USER
   ```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | none, required | API key for the Gemini API |
| `PORT` | `8432` | port the HTTP server binds |
| `ENV_FILE` | `~/.env` | file the key is read from when the variable is not set |
| `SKILL_ID` | empty | if set, requests from any other Alexa skill id are rejected with HTTP 400 |
| `API_KEY` | empty | shared secret accepted in an `X-Api-Key` header for your own scripts and uptime checks |
| `ENFORCE_SIGNATURE` | `0` | when truthy (`1`, `true`, `yes`, `on`), reject requests that are neither signed by Amazon nor carrying a valid `X-Api-Key` |

## Connecting it to Alexa

1. Create a skill in the Alexa developer console, choosing "Custom" and "Provision your own".
2. Paste `interaction_model.json` into the Interaction Model JSON editor and build it.
3. Set the endpoint to your HTTPS URL ending in `/alexa`.
4. Enable testing.

### Gotchas

- A development-stage skill only runs on Echo devices registered to the **same Amazon account** as the developer account. Mismatched accounts fail in a way that looks exactly like a broken endpoint.
- Amazon rejects invocation names that are a single word, so `toby` will not save; use two words.
- Every interaction-model sample needs a carrier phrase such as "tell me". Alexa absorbs the carrier, so the server receives a fragment like "the boiling point of tungsten" rather than a full question. The system prompt accounts for this.
- Amazon gives the endpoint 8 seconds total to respond.

## Security notes

- Requests are authenticated in one of two ways: a valid Amazon request signature, or an `X-Api-Key` header matching `API_KEY`.
- Amazon does not send custom headers, so an API key cannot be used to gate genuine Alexa traffic. Signature verification is the protocol-level equivalent and is the mechanism used for real Alexa requests.
- Signature verification validates the certificate URL against `s3.amazonaws.com/echo.api/`, checks that the certificate covers `echo-api.amazon.com`, verifies the signature over the raw request body, and rejects requests whose `request.timestamp` is more than 150 seconds out of date.
- `SKILL_ID` additionally pins the endpoint to a single skill.
- The certificate chain is only checked for hostname, validity and signature. The chain is **not** validated against a pinned root CA beyond what `openssl` does by default.
- Speech output is sanitised and length-capped.
- The model call has a hard timeout and falls back to a spoken apology rather than erroring.
- The API key is read from the environment or a dotenv file and is never logged or echoed.

### Rolling out enforcement

Leave `ENFORCE_SIGNATURE=0` for the first day. Requests that carry Amazon's signature headers are
still verified, but a failure is logged and allowed through rather than rejected, so a bug in the
verifier cannot take the skill offline. Watch the log for `signature verification failed (soft mode)`:

```sh
journalctl --user -u alexa-toby.service -f | grep -i signature
```

Once real Alexa traffic is arriving with no soft-mode warnings, set `ENFORCE_SIGNATURE=1` and
restart. From then on anything unsigned is rejected with `401`.

## Limitations

- It answers questions only, and cannot control other smart-home devices because custom skills have no device-control API.
- It never receives raw audio.
- Answer quality depends on the model, and smaller models are unreliable for precisely recalled figures.

## License

MIT, see LICENSE.
