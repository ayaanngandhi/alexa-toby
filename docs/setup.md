# Setup guide

Everything needed to stand up your own copy of this skill, plus notes on how this particular
deployment is configured.

The short version: one Python file with no dependencies, exposed over HTTPS, wired into a custom Alexa
skill. The hard part is not the server — it is the Alexa console, and specifically the certificate
setting.

---

## 1. Prerequisites

- An Amazon developer account, **created with the same Amazon account your Echo devices are on**.
  A development-stage skill only runs on devices registered to the developer account. This is the
  single most common reason a skill appears to do nothing.
- A machine to run the service. Anything with Python 3.9+, including a Raspberry Pi.
- A Gemini API key with access to a Gemma model.
- A way to expose a port over HTTPS. A Cloudflare Tunnel is used here because it needs no open
  inbound ports and issues a publicly trusted certificate.

---

## 2. Run the service

```sh
git clone https://github.com/ayaanngandhi/alexa-toby.git
cd alexa-toby
cp .env.example .env      # then edit and set GEMINI_API_KEY
python3 alexa_toby.py     # listens on 0.0.0.0:8432
```

Point it at your dotenv file if it is not `~/.env`:

```sh
ENV_FILE=/path/to/.env python3 alexa_toby.py
```

Check it:

```sh
curl -s localhost:8432/health          # {"status": "ok"}
```

### As a service

`alexa-toby.service` is included. Adjust the paths and install it as a **user** unit:

```sh
mkdir -p ~/.config/systemd/user
cp alexa-toby.service ~/.config/systemd/user/
systemctl --user enable --now alexa-toby.service
loginctl enable-linger "$USER"     # keeps it running without a login session
```

---

## 3. Expose it over HTTPS

Requirements Alexa enforces: public HTTPS on port 443, a certificate from a trusted CA, a complete
chain, and a response within **8 seconds**.

With Cloudflare Tunnel, add a public hostname pointing at `http://localhost:8432`. That creates the
DNS record automatically.

Verify from outside your network before touching the Alexa console:

```sh
curl -s https://your.host.name/health
```

If that does not return `{"status":"ok"}`, stop — the skill cannot work either.

---

## 4. Create the skill

1. Go to <https://developer.amazon.com/alexa/console/ask> with the account your Echoes use.
2. **Create Skill** → name it → **Custom** → **Provision your own** (not Alexa-hosted).
   Alexa-hosted gives you a Lambda and no endpoint URL, so your server is never contacted.
3. **Interaction Model → JSON Editor** → paste `interaction_model.json` → **Save Model** → **Build Model**.
4. **Endpoint → HTTPS** → paste your URL → choose the certificate option → **Save Endpoints**.
   The certificate step is where people lose hours; see the next section.
5. **Test** tab → type an utterance → check **Skill I/O** for the request and response.

### Invocation name rules

- A single word is rejected without proof of brand ownership. `Toby` will not save; `Toby Assistant` will.
- Every sample utterance needs a carrier phrase. A sample that is only a slot is invalid, so use
  `ask {question}` rather than a bare `{question}`. Alexa strips the carrier, so
  *"ask Toby Assistant what is the boiling point of tungsten"* reaches your server as
  `the boiling point of tungsten`.

---

## 5. The certificate trap (read this)

There are two certificate options, and **choosing the wrong one produces total silent failure**.

- `My development endpoint has a certificate from a trusted certificate authority`
- `My development endpoint is a sub-domain of a domain that has a wildcard certificate from a certificate authority`

If your endpoint uses a **wildcard certificate** — Cloudflare's Universal SSL is `*.example.com` —
you **must** pick the **wildcard** option.

Pick "trusted certificate authority" with a wildcard cert and Amazon rejects the connection during
its own TLS validation. The consequence is easy to misdiagnose:

- Alexa says *"I am unable to reach the requested skill."*
- **Nothing arrives at your web server.** No log line, no 4xx, nothing at the CDN edge.
- The domain resolves, `curl` succeeds from every machine you own, the certificate chain is valid.

That combination — everything checks out locally, yet zero requests arrive — almost always means
the certificate option is wrong.

The correct configuration for a wildcard certificate:

```json
"endpoint": {"sslCertificateType": "Wildcard", "uri": "https://your.host.name/alexa"}
```

### Debugging method that actually works

Watch the server, then invoke the skill:

```sh
journalctl --user -u alexa-toby.service -f
```

A line appearing there proves Amazon reached you. No line means the problem is on the Amazon side,
and no amount of server debugging will help. Do not trust the console's Test tab as evidence — it
will show you a request JSON even when nothing was ever sent.

---

## 6. Lock it down

The URL is not a secret, so the endpoint authenticates callers.

| Caller | Proof required |
| --- | --- |
| Alexa | valid Amazon request signature |
| Your own scripts | `X-Api-Key` header |
| Anyone else | rejected 401 |

Amazon does **not** let you attach custom headers to a skill request, so an API key can never gate
genuine Alexa traffic. Signature verification is the protocol-level equivalent: the certificate URL
must resolve under `s3.amazonaws.com/echo.api/`, the certificate must cover `echo-api.amazon.com`,
the signature must verify over the raw body, and the request timestamp must be within 150 seconds.

Config lives in a mode-600 env file, never in the unit or the repo:

```sh
API_KEY=<generated secret>
SKILL_ID=amzn1.ask.skill.<your-skill-id>
ENFORCE_SIGNATURE=0
```

**Roll out enforcement gradually.** Real Alexa requests always carry signature headers, so a bug in
the verifier would otherwise break a working skill. With `ENFORCE_SIGNATURE=0`, a failed check is
logged and allowed through. Watch for soft-mode warnings, then set it to `1` and restart.

---

## 7. Other languages, including Hindi

Language support has two halves, and both are required.

**Server side** — already handled. The system prompt tells the model to answer in the language the
question is asked in, and a `hi-IN` request adds an explicit instruction to reply in Hindi. A Hindi
question returns a Hindi answer in Devanagari even when the locale is `en-IN`.

**Alexa side** — the skill needs a locale for the language, otherwise the request never reaches you:

1. **Build → Language Settings** (or the locale dropdown beside the skill name) → add **Hindi (IN)**.
2. Paste the interaction model with Hindi sample utterances, then **Build Model**.
3. The endpoint is set globally under **Default Region**, so a new locale inherits it. If you set
   regional endpoints instead, set the certificate option for each region.

Region note: India and Europe both use the **Europe and India** region. The device's `apiEndpoint`
in a request will show `api.eu.amazonalexa.com` for Indian devices.

Sample Hindi utterances to add alongside the English ones:

```json
{"name": "AskIntent", "slots": [{"name": "question", "type": "AMAZON.SearchQuery"}],
 "samples": ["पूछो {question}", "बताओ {question}", "क्या है {question}", "{question} के बारे में बताओ"]}
```

---

## 8. Troubleshooting

| Symptom | Cause |
| --- | --- |
| *"Unable to reach the requested skill"*, zero server traffic | Certificate option does not match the certificate type (wildcard vs trusted) |
| Console Test tab falls back immediately | Utterance did not match; wrong invocation name, or a sample without a carrier phrase |
| Request arrives, answer is a spoken apology | Model call timed out or API key is wrong; check the service log |
| Nothing works, no traffic, config looks perfect | Skill and Echo are on different Amazon accounts |
| Works in the simulator, not on the device | Interaction model not built, or the skill is not in Development stage |

Length and time limits: 8 seconds to respond, 8,000 characters of speech, 120 KB response body.
Progressive responses do not extend the 8 second budget.

---

## 9. How this deployment is set up

Concrete details for the running instance, so future maintenance does not need to rediscover them.

- **Host** — home server, `~/projects/alexa-toby`, service `alexa-toby.service` as a systemd user
  unit with linger enabled. Listens on `:8432`.
- **Public hostnames** — `alexa.ayaanngandhi.in` and `toby.ayaanngandhi.in`, both routed to
  `http://localhost:8432` through the existing dashboard-managed Cloudflare Tunnel. No inbound ports
  are opened; the tunnel dials out.
- **Certificate** — Cloudflare Universal SSL, i.e. `*.ayaanngandhi.in`. The skill is therefore set to
  `sslCertificateType: "Wildcard"`. This is the setting that made it work after hours of debugging.
- **Skill** — `Toby Assistant`, id `amzn1.ask.skill.f4e102b7-6185-4b56-af1b-bc6452d185c0`, locale
  `en-IN` in the Europe/India region. Invocation is `Toby Assistant` because Amazon rejects the
  single word `Toby`. The Echoes are on a family Amazon account, so the developer console was created
  against that same account.
- **Model** — `gemma-4-26b-a4b-it` through the Gemini API, `thinkingLevel: minimal`, temperature 0.2,
  `maxOutputTokens` 500. A dense 31B model was measured at 77 s per call and is unusable here; the
  26B A4B mixture sits at 1.5–2.5 s.
- **Guard** — the model call is capped at 5.5 s, comfortably inside Alexa's 8 s budget. On timeout the
  skill speaks an apology instead of failing.
- **Secrets** — Gemini key in `/home/mac/.env`; API key, skill id and the enforcement flag in
  `~/.config/alexa-toby/env` at mode 600, loaded by a systemd drop-in.
- **Verification** — after any change, replay a real request instead of trusting the console:

```sh
KEY=$(grep '^API_KEY=' ~/.config/alexa-toby/env | cut -d= -f2)
curl -s -X POST https://alexa.ayaanngandhi.in/alexa \
  -H 'Content-Type: application/json' -H "X-Api-Key: $KEY" \
  -d '{"version":"1.0","session":{"application":{"applicationId":"amzn1.ask.skill.f4e102b7-6185-4b56-af1b-bc6452d185c0"}},"request":{"type":"LaunchRequest"}}'
```
