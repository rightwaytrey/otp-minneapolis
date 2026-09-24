#!/usr/bin/env python3
"""test-config-ladder-locations.py — the `location` comparison in
check-config-ladder.py --deployed (backlog 30.1), on fixture snippets, with no
ssh and no /etc/nginx.

    python3 scripts/test-config-ladder-locations.py

Cases: identical, missing, extra (the pure comparison); a `location` inside a
comment is not a location; and check_deployed end to end with read_host
replaced, so the FAIL is proved to name each drifted location and the right
install command for the house and for prod.
"""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ladder", HERE / "check-config-ladder.py")
ladder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ladder)

FIXTURE = """\
# a comment that says location /not-a-location { is not a block
server_tokens off;
location /api/debug-log {
    client_max_body_size 1536k;
    proxy_pass http://127.0.0.1:8092;
}
location = /unlock {
    return 204;
}
location /api/places {
    proxy_pass http://127.0.0.1:8092;
}
"""


def drop_location(text, spec):
    """The fixture with one `location <spec> { ... }` block cut out."""
    start = text.index(f"location {spec} {{")
    end = text.index("}\n", start) + 2
    return text[:start] + text[end:]


class LocationDrift(unittest.TestCase):
    def test_identical(self):
        s = ladder.installed_locations(FIXTURE)
        self.assertEqual(ladder.location_drift(s, set(s)), ([], []))

    def test_comment_is_not_a_location(self):
        self.assertEqual(
            ladder.installed_locations(FIXTURE),
            {"/api/debug-log", "= /unlock", "/api/places"},
        )

    def test_missing(self):
        rendered = ladder.installed_locations(FIXTURE)
        installed = ladder.installed_locations(drop_location(FIXTURE, "/api/places"))
        self.assertEqual(ladder.location_drift(rendered, installed), (["/api/places"], []))

    def test_extra(self):
        rendered = ladder.installed_locations(drop_location(FIXTURE, "= /unlock"))
        installed = ladder.installed_locations(FIXTURE)
        self.assertEqual(ladder.location_drift(rendered, installed), ([], ["= /unlock"]))

    def test_env_for_ssh(self):
        self.assertEqual(ladder.env_for_ssh("local"), "house")
        self.assertEqual(ladder.env_for_ssh("rwt@100.126.171.72"), "prod")


class CheckDeployedEndToEnd(unittest.TestCase):
    """check_deployed with the host replaced by the REAL render of this
    checkout's template (so the ladder rungs and every other check pass) and
    then one location cut out or one added."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        js = root / "debug-log.js"
        js.write_text("const MAX_FULL_PAYLOAD_CHARS = 1000000\nconst MAX_BODY_BYTES = 1400000\n")
        py = root / "preferences_api.py"
        py.write_text("DEBUG_LOG_MAX_LINE_CHARS = 1179648\n")
        self.saved = (ladder.DEBUG_LOG_JS, ladder.PREFS_API, ladder.read_host)
        ladder.DEBUG_LOG_JS, ladder.PREFS_API = js, py
        self.prefs = py.read_text()

    def tearDown(self):
        ladder.DEBUG_LOG_JS, ladder.PREFS_API, ladder.read_host = self.saved
        self.tmp.cleanup()

    def render(self, env):
        rn = ladder._load_renderer()
        return rn.render_one(rn.TMPL_DIR / "otp-common.conf.tmpl", env,
                             rn.resolve(env, True), mask=True)

    def run_check(self, nginx_text, ssh):
        meta = {"unit_state": "active", "unit_started": "x",
                "unit_started_epoch": "200", "prefs_mtime": "100"}
        ladder.read_host = lambda _ssh: (nginx_text, self.prefs, meta)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ladder.check_deployed(ssh)
        return rc, out.getvalue() + err.getvalue()

    def test_house_identical_passes(self):
        rc, out = self.run_check(self.render("house"), "local")
        self.assertEqual(rc, 0, out)
        self.assertIn("0 missing, 0 extra", out)

    def test_house_missing_names_location_and_install(self):
        text = self.render("house")
        start = text.index("location /api/places {")
        end = text.index("\n    }\n", start) + len("\n    }\n")
        rc, out = self.run_check(text[:start] + text[end:], "local")
        self.assertEqual(rc, 1, out)
        self.assertIn("missing on the box: location /api/places", out)
        self.assertIn("sudo ./install-house-nginx.sh --install", out)
        self.assertNotIn("deploy-app.sh", out)

    def test_prod_extra_names_location_and_install(self):
        text = self.render("prod") + "\nlocation /api/stale-thing {\n    return 404;\n}\n"
        rc, out = self.run_check(text, "rwt@100.126.171.72")
        self.assertEqual(rc, 1, out)
        self.assertIn("extra on the box (not in the `prod` render): location /api/stale-thing", out)
        self.assertIn("./deploy-app.sh 100.126.171.72 --only nginx", out)
        self.assertNotIn("install-house-nginx", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
