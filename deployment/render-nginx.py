#!/usr/bin/env python3
"""render-nginx.py — the ONE place an nginx config for this project is produced.

There used to be two hand-maintained copies of the same site:

    config/nginx/otp-common.conf      the house/desktop copy
    deployment/nginx/otp-common.conf  what deploy-app.sh installed on the Linode

They were 168 diff lines apart on 2026-09-01 and nothing kept them honest but a
separate checker (scripts/check-nginx-parity.py) that compared three directives
on the locations they happened to share. A fix applied to one was not deployed
until someone remembered the other — which is how the 2026-08-27 CORS/429 fix
sat undeployed for three days with every health check green.

Now there is one template per file and one value set per environment:

    deployment/nginx/otp.conf.tmpl
    deployment/nginx/otp-common.conf.tmpl
    deployment/env/house.env      rwtpc4 (the desktop)
    deployment/env/prod.env       the Linode

Parity on everything the two environments share is a property of the source,
not of a checker: the shared text is literally the same bytes. `--check`
verifies exactly that, and is what replaced check-nginx-parity.py.

TEMPLATE SYNTAX
    __NAME__            substituted from the environment's value set, or from
                        the process environment for names declared SECRET.
    #@if house          conditional region. `#@if <env>[,<env>...]`,
    #@else              optional `#@else`, closed by `#@endif`. The `#@` prefix
    #@endif             keeps the raw template parseable as nginx comments.
    #@one-sided <spec> :: <reason>
                        header declaration: this `location` spec is expected to
                        render in only some environments, and why. An
                        undeclared one-sided location fails --check. That is the
                        shape every drift so far has had.

FAIL CLOSED
    A rendered file that still contains `__` is an error, always. The Stadia key
    and the unlock secret are substituted here and must never be committed:
    otp-minneapolis is a PUBLIC repo, and a secret already reached it this way
    and sat public from 2026-06-01 to 2026-09-01.

USAGE
    render-nginx.py --env prod  --out DIR    # secrets required in the environ
    render-nginx.py --env house --out DIR
    render-nginx.py --check                  # renders both, asserts parity,
                                             # and scans for committed secrets
    render-nginx.py --env prod --out DIR --placeholder-secrets
                                             # dummy secrets; NEVER install this

Exit: 0 ok, 1 failed, 75 SKIP (inputs unresolvable — loudly). `--check`
returns 75 when deployment/.env is absent: parity still held, but the
committed-secret guard had no values to compare and must not read as green.
"""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

SKIP = 75

DEPLOYMENT = Path(__file__).resolve().parent
TMPL_DIR = DEPLOYMENT / "nginx"
ENV_DIR = DEPLOYMENT / "env"
TEMPLATES = ["otp.conf.tmpl", "otp-common.conf.tmpl"]
ENVIRONMENTS = ["house", "prod"]

# What a secret looks like when we are only rendering to compare shapes. Long
# enough that map_hash_bucket_size behaviour is representative, and obviously
# not a real credential.
DUMMY = "PLACEHOLDER-NOT-A-REAL-SECRET-0000000000000000"

# deploy-app.sh exports some deployment/.env names under the template's name.
# The committed-secret scan has to know about the rename, or the value it is
# actually protecting goes unchecked.
SECRET_SOURCE_ALIASES = {"RIDE_UPSTREAM": ["HOME_TAILSCALE_IP"]}


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def die_skip(msg):
    print(f"SKIP: {msg}", file=sys.stderr)
    print("SKIP: nothing was rendered and nothing was verified.", file=sys.stderr)
    sys.exit(SKIP)


# --------------------------------------------------------------------------
# value sets


def load_env(name):
    """Parse deployment/env/<name>.env.

    Lines are `KEY=value`, `KEY=SECRET` (value must come from the process
    environment), `#` comments and blanks. No shell, no expansion — this file is
    read by Python and by nobody else, so it does not get to run anything.
    """
    path = ENV_DIR / f"{name}.env"
    if not path.is_file():
        die_skip(f"value set not found at {path}")
    values, secrets = {}, []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            die(f"{path}:{lineno}: expected KEY=value, got {raw!r}")
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            die(f"{path}:{lineno}: key {key!r} is not [A-Z0-9_]+")
        if val == "SECRET":
            secrets.append(key)
        else:
            values[key] = val
    return values, secrets


def resolve(name, use_dummy_secrets):
    values, secrets = load_env(name)
    missing = []
    for key in secrets:
        got = os.environ.get(key, "")
        if not got:
            if use_dummy_secrets:
                got = DUMMY
            else:
                missing.append(key)
                continue
        values[key] = got
    if missing:
        die(
            f"env `{name}` declares these as SECRET and they are empty in the "
            "environment: " + ", ".join(missing) + ".\n"
            "       deploy-app.sh sources deployment/.env and exports them; if "
            "you are running this by hand, export them yourself.\n"
            "       Do NOT put them in deployment/env/*.env — that file is "
            "COMMITTED to a public repo."
        )
    return values


# --------------------------------------------------------------------------
# template expansion


COND_RE = re.compile(r"^#@(if|else|endif)\b\s*(.*)$")
ONE_SIDED_RE = re.compile(r"^#@one-sided\s+(.+?)\s*::\s*(.+)$")


def declared_one_sided(text):
    """{location spec -> reason} from the template's `#@one-sided` header lines."""
    out = {}
    for line in text.splitlines():
        m = ONE_SIDED_RE.match(line.strip())
        if m:
            out[re.sub(r"\s+", " ", m.group(1))] = m.group(2)
    return out


def expand_conditionals(text, env, path):
    """Keep the branches that apply to `env`; drop the rest, and the markers."""
    out = []
    # stack of (emitting_now, this_if_has_already_matched)
    stack = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        m = COND_RE.match(stripped)
        if not m:
            # `#@one-sided` is a declaration for --check, not content: it would
            # otherwise ship to /etc/nginx as noise.
            if ONE_SIDED_RE.match(stripped):
                continue
            if all(active for active, _ in stack):
                out.append(line)
            continue
        kind, arg = m.group(1), m.group(2).strip()
        if kind == "if":
            envs = [e.strip() for e in arg.split(",") if e.strip()]
            if not envs:
                die(f"{path}:{lineno}: `#@if` with no environment")
            for e in envs:
                if e not in ENVIRONMENTS:
                    die(
                        f"{path}:{lineno}: unknown environment {e!r} "
                        f"(known: {', '.join(ENVIRONMENTS)})"
                    )
            outer = all(active for active, _ in stack)
            matched = env in envs
            stack.append((outer and matched, matched))
        elif kind == "else":
            if not stack:
                die(f"{path}:{lineno}: `#@else` with no open `#@if`")
            _, matched = stack[-1]
            outer = all(active for active, _ in stack[:-1])
            stack[-1] = (outer and not matched, True)
        else:  # endif
            if not stack:
                die(f"{path}:{lineno}: `#@endif` with no open `#@if`")
            stack.pop()
    if stack:
        die(f"{path}: {len(stack)} unclosed `#@if` block(s)")
    return "\n".join(out) + "\n"


def substitute(text, values, path, mask=False):
    def repl(m):
        key = m.group(1)
        if key not in values:
            die(
                f"{path}: template uses __{key}__ but no value set supplies it. "
                f"Add it to deployment/env/*.env (as SECRET if it is one)."
            )
        # --check renders every environment with the same masked token, so a
        # value that legitimately differs per host (RIDE_UPSTREAM) does not read
        # as drift, while a different KEY still does.
        return f"<{key}>" if mask else values[key]

    return re.sub(r"__([A-Z0-9_]+)__", repl, text)


BANNER = """\
# GENERATED FILE — rendered from {tmpl} for env `{env}` by
# deployment/render-nginx.py. DO NOT EDIT IN PLACE: a hand-edit here is
# invisible to every check in the repo and is silently reverted by the next
# deploy. Edit the template, re-render, and let deploy-app.sh install it.
"""


def render_one(tmpl_path, env, values, mask=False):
    raw = tmpl_path.read_text(encoding="utf-8")
    body = expand_conditionals(raw, env, tmpl_path)
    body = substitute(body, values, tmpl_path, mask=mask)
    # Fail closed. This is the rule deploy-app.sh:231 carried as a comment and
    # that got violated anyway; it is code now, on the only path that renders.
    stray = re.findall(r"__[A-Za-z0-9_]*__", body)
    if stray:
        die(
            f"{tmpl_path.name} ({env}): unsubstituted placeholder(s) remain: "
            + ", ".join(sorted(set(stray)))
        )
    return BANNER.format(tmpl=f"deployment/nginx/{tmpl_path.name}", env=env) + body


def render(env, out_dir, use_dummy_secrets=False, mask=False):
    values = resolve(env, use_dummy_secrets or mask)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in TEMPLATES:
        tmpl = TMPL_DIR / name
        if not tmpl.is_file():
            die_skip(f"template not found at {tmpl}")
        body = render_one(tmpl, env, values, mask=mask)
        dest = out_dir / name[: -len(".tmpl")]
        dest.write_text(body, encoding="utf-8")
        dest.chmod(0o600 if not use_dummy_secrets else 0o644)
        written.append(dest)
    return written


# --------------------------------------------------------------------------
# --check: parity is a property of the source, so prove it


def strip_comments(text):
    """Drop `#` comments. Without this, the word "location" inside a comment is
    parsed as a location block — which is exactly what it did on the first run.
    """
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


def parse_locations(text):
    """{spec -> block body}, brace-matched, for `location` blocks."""
    text = strip_comments(text)
    out = {}
    for m in re.finditer(r"location\s+([^{]+?)\s*\{", text):
        spec = re.sub(r"\s+", " ", m.group(1).strip())
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        if spec in out:
            die(f"duplicate `location {spec}` in a rendered config")
        out[spec] = text[m.end() : i]
    return out


class ValueScan:
    """What guard (2) of check_no_secret_is_committed actually compared.

    A green line has to distinguish "no secret leaked" from "there was nothing
    to compare against" — see the class docstring there.
    """

    def __init__(self, dotenv, ran):
        self.dotenv = dotenv
        self.ran = ran
        self.declared = set()  # SECRET names + their .env source aliases
        self.names = set()  # names whose value was actually compared
        self.too_short = set()  # in .env but < 8 chars, so not credential-like
        self.files = set()  # committed files searched

    def report(self):
        rel = self.dotenv.name
        if not self.ran:
            return [
                f"committed-secret scan: NOT RUN — deployment/{rel} is absent, "
                "so no value set was compared.",
                "  (correct in a git worktree, where .env is gitignored; on a "
                "deploy host it means the file is missing.)",
            ]
        lines = [
            "committed-secret scan: "
            f"{len(self.names)} value(s) compared against {len(self.files)} "
            "committed file(s).",
            "  values : " + (", ".join(sorted(self.names)) or "none"),
            "  files  : " + (", ".join(sorted(self.files)) or "none"),
        ]
        skipped = sorted(self.declared - self.names - self.too_short)
        if self.too_short:
            lines.append(
                "  short  : "
                + ", ".join(sorted(self.too_short))
                + " (< 8 chars, not credential-like)"
            )
        if skipped:
            lines.append(
                "  absent : " + ", ".join(skipped) + f" (not set in deployment/{rel})"
            )
        return lines


def check_no_secret_is_committed():
    """Refuse to let a real credential live in a committed file.

    Two independent guards, because prose did not work: deploy-app.sh:231
    carried "it must never be committed into config/nginx" as a comment and the
    unlock secret was committed anyway, and sat in a PUBLIC repo from 2026-06-01
    to 2026-09-01.

      1. Every name any value set declares SECRET must still appear as a
         `__NAME__` placeholder in some template. Pasting a literal value over
         the placeholder makes the placeholder vanish, and this notices.
      2. If deployment/.env is readable, the actual value behind each of those
         names must not appear in any template or value set. That catches the
         paste that keeps the placeholder somewhere else in the file. Only
         SECRET-declared names are scanned: DOMAIN is a public hostname and
         belongs in `server_name`, and scanning every .env key just trains
         people to ignore the check.

    Guard (2) needs deployment/.env, which is gitignored and therefore absent
    from every git worktree. It used to be silently skipped there, so `--check`
    printed the same `OK` whether it had compared every secret or none of them.
    Returns (failures, scan) instead, where `scan` says exactly which value
    names and which files guard (2) compared — and whether it ran at all. The
    caller turns "did not run" into exit 75 SKIP, not 0.
    """
    failures = []
    tmpl_text = {
        name: (TMPL_DIR / name).read_text(encoding="utf-8")
        for name in TEMPLATES
        if (TMPL_DIR / name).is_file()
    }
    all_tmpl = "\n".join(tmpl_text.values())

    secret_names = set()
    for env in ENVIRONMENTS:
        _, secrets = load_env(env)
        secret_names.update(secrets)
    for key in sorted(secret_names):
        if f"__{key}__" not in all_tmpl:
            failures.append(
                f"{key} is declared SECRET in a value set but no template "
                f"references __{key}__. Either the placeholder was replaced with "
                "a literal value (do NOT commit that) or the declaration is stale."
            )

    scan_names = set(secret_names)
    for key in list(secret_names):
        scan_names.update(SECRET_SOURCE_ALIASES.get(key, []))

    dotenv = DEPLOYMENT / ".env"
    scan = ValueScan(dotenv=dotenv, ran=dotenv.is_file())
    if dotenv.is_file():
        haystack = {**tmpl_text}
        for env in ENVIRONMENTS:
            f = ENV_DIR / f"{env}.env"
            if f.is_file():
                haystack[f"env/{env}.env"] = f.read_text(encoding="utf-8")
        # env.example is committed and sits right next to .env, which is exactly
        # how it ended up carrying the real HOME_TAILSCALE_IP.
        example = DEPLOYMENT / "env.example"
        if example.is_file():
            haystack["env.example"] = example.read_text(encoding="utf-8")
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key not in scan_names:
                continue
            # Short values are not credentials and would false-positive against
            # ordinary words (APP_PORT=9966, APP_USER=rwt).
            if len(val) < 8:
                scan.too_short.add(key)
                continue
            scan.names.add(key)
            for where, text in haystack.items():
                if val in text:
                    failures.append(
                        f"the value of {key} from deployment/.env appears "
                        f"verbatim in {where}. That file is COMMITTED to a public "
                        "repo. Put the placeholder back."
                    )
        scan.files.update(haystack)
        scan.declared = set(scan_names)
    return failures, scan


def check():
    """Render every environment with dummy secrets and assert:

    1. every location the environments SHARE is byte-identical, and
    2. every one-sided location is declared `#@one-sided` in its template.

    (1) is what check-nginx-parity.py used to approximate by comparing three
    directives; here it is total, because the shared text comes from the same
    template bytes. (2) is the drift shape: a location that exists on one host
    and quietly does not on the other.
    """
    failures, scan = check_no_secret_is_committed()
    rendered = {}
    with tempfile.TemporaryDirectory() as tmp:
        for env in ENVIRONMENTS:
            out = Path(tmp) / env
            for path in render(env, out, mask=True):
                rendered.setdefault(path.name, {})[env] = path.read_text(
                    encoding="utf-8"
                )

        for fname, per_env in sorted(rendered.items()):
            tmpl_text = (TMPL_DIR / (fname + ".tmpl")).read_text(encoding="utf-8")
            declared = declared_one_sided(tmpl_text)
            locs = {env: parse_locations(text) for env, text in per_env.items()}
            all_specs = set().union(*(set(v) for v in locs.values()))
            shared = sorted(s for s in all_specs if all(s in v for v in locs.values()))
            one_sided = sorted(s for s in all_specs if s not in shared)

            for spec in shared:
                bodies = {env: locs[env][spec] for env in locs}
                if len(set(bodies.values())) != 1:
                    where = ", ".join(
                        f"{env} has {len(b.splitlines())} lines" for env, b in bodies.items()
                    )
                    failures.append(
                        f"{fname}: location {spec} renders differently per "
                        f"environment ({where}) — it must be identical or be "
                        "declared #@one-sided. A location inside a #@if is how "
                        "the old two-copy drift comes back."
                    )
            for spec in one_sided:
                present = sorted(env for env in locs if spec in locs[env])
                if spec not in declared:
                    failures.append(
                        f"{fname}: location {spec} renders only for "
                        f"{'/'.join(present)} and is not declared. Add "
                        f"`#@one-sided {spec} :: <reason>` to the template "
                        "header, or make it unconditional."
                    )
            for spec in declared:
                if spec in shared:
                    failures.append(
                        f"{fname}: `#@one-sided {spec}` is stale — it now "
                        "renders in every environment. Remove the declaration."
                    )
                elif spec not in all_specs:
                    failures.append(
                        f"{fname}: `#@one-sided {spec}` is stale — it renders "
                        "in no environment at all. Remove the declaration."
                    )

            print(
                f"{fname}: {len(shared)} shared location(s) byte-identical, "
                f"{len(one_sided)} one-sided ({', '.join(one_sided) or 'none'})"
            )

    print()
    for line in scan.report():
        print(line)

    if failures:
        print("\nFAIL: the templates did not pass.", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    # Parity held, but half the check did not run. Exit 75 so the caller can
    # tell the two apart: nightly-verify.sh files it as SKIP, and deploy-app.sh
    # aborts (a deploy host always has deployment/.env, so a SKIP there means
    # the file is gone, not that the check is inapplicable).
    if not scan.ran:
        print(
            "\nSKIP: shared locations are identical, but the committed-secret "
            "guard had nothing to compare against.",
            file=sys.stderr,
        )
        return SKIP
    print("\nOK: every shared location is identical by construction.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", choices=ENVIRONMENTS, help="which value set to render")
    ap.add_argument("--out", type=Path, help="directory to write the rendered files to")
    ap.add_argument(
        "--check",
        action="store_true",
        help="render every environment and assert shared locations are identical",
    )
    ap.add_argument(
        "--placeholder-secrets",
        action="store_true",
        help="render with obviously-fake secrets, for inspection. NEVER install the result.",
    )
    args = ap.parse_args()

    if args.check:
        return check()
    if not args.env or not args.out:
        ap.error("--env and --out are required unless --check is given")
    written = render(args.env, args.out, use_dummy_secrets=args.placeholder_secrets)
    for p in written:
        print(p)
    if args.placeholder_secrets:
        print(
            "\nNOTE: rendered with placeholder secrets. This output is for "
            "inspection only — installing it would break the unlock gate and "
            "geocoding.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
