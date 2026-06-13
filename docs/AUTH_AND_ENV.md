# Auth-Gated Apps & Env/Secret Injection

RenderPR can inject environment variables/secrets into the preview build and get
past login walls so it reviews the real app instead of a blank or login page.

See the design discussion in [`FUTURE.md`](./FUTURE.md) → "Auth-Gated Apps &
Env/Secret Injection" and tracking issue #32.

## How it works

1. **Env injection** — RenderPR reads `.env.example` (and/or `.renderpr.yml`'s
   `env` block) to learn required vars, then injects the user's stored secret
   values both as an ephemeral `.env.local` and into the dev-server process env,
   *before* the build/dev server starts.
2. **Synthetic-session auth** — instead of capturing a real user's session,
   RenderPR mints a session for a **synthetic** user, either by **forging** a
   token from the app's own signing secret or by calling the provider's **admin
   API**. The session is loaded into the browser before navigating. OAuth
   providers (Google/GitHub/SSO) are never scripted — the app/provider
   self-issues the session we mint.
3. **Login-wall guard** — if a page still redirects to a login wall and no auth
   was configured, RenderPR degrades the progress comment with guidance instead
   of reviewing a login screen.

**Security:** secrets are **never** injected on fork PRs (enforced in code), are
never logged, and only ever mint sessions for a synthetic user.

## Storing secrets (BYOC)

Secrets live in your own AWS, one SSM SecureString per secret under
`/renderpr/secrets/{installation_id}/{owner}/{repo}/{KEY}`:

```bash
scripts/renderpr-secrets.sh 12345 acme/web NEXT_PUBLIC_API_URL=https://api.acme.dev
scripts/renderpr-secrets.sh 12345 acme/web NEXTAUTH_SECRET="$(openssl rand -hex 32)"
scripts/renderpr-secrets.sh 12345 acme/web --from-file .env.preview
scripts/renderpr-secrets.sh --list 12345 acme/web
```

## `.renderpr.yml`

All keys optional; the file layers over auto-detection.

```yaml
env:
  from: .env.example            # which example file to read required vars from
  vars: [NEXT_PUBLIC_API_URL]   # explicit var names (atop/instead of `from`)

auth:
  type: nextauth                # nextauth | jwt | supabase | clerk | auth0 | firebase
  user:                         # claims baked into the synthetic session
    email: preview@renderpr.dev
    name: Preview User
    role: admin
```

The config only *declares* which secrets/method to use — the secret **values**
always live in SSM, never in the file.

## Per-provider support

| `auth.type` | Required secret(s) | Extra `auth` keys | How |
|---|---|---|---|
| `nextauth` | `NEXTAUTH_SECRET` (or `AUTH_SECRET`) | `version: v5\|v4`, `cookieName` | Forge the encrypted session JWE. Handles Google/GitHub logins. |
| `jwt` | the named secret (default `JWT_SECRET`) | `secret`, `name`, `storage: cookie\|localStorage`, `algorithm` | Forge a self-signed JWT. |
| `supabase` | `SUPABASE_JWT_SECRET` *or* `SUPABASE_SERVICE_ROLE_KEY` | `baseUrl` (or `NEXT_PUBLIC_SUPABASE_URL` secret) | Forge (symmetric) or GoTrue admin API. |
| `clerk` | `CLERK_SECRET_KEY` | `userId` (required), `signInPath` | Backend sign-in ticket, consumed at an entry URL. |
| `firebase` | `FIREBASE_SERVICE_ACCOUNT`, an API key | `apiKey` | Admin custom token → Identity Toolkit exchange (best-effort localStorage). |

### Known boundaries

- Managed providers with no admin/test API and fully remote-validated keys are
  not supported this iteration.
- Database-session strategies (the cookie is a DB lookup key, not a JWT) need a
  real backend — out of scope here.
- Forging tracks each library's token format; NextAuth v4 and Auth.js v5 are
  both covered and validated against the real libraries.
