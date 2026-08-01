from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
import io
import os
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from scripts import publish
from scripts.rpsl import (
    RADB_REVIEWED_DESCRIPTION,
    parse_text,
    transform_for_radb,
)


MAINTAINER = "MAINT-MOEDB"
EMAIL = "publish@example.net"
DATE = "20240102"
REASON = "approved Git deletion"

ROLE = """\
role:           Example NOC
address:        Example address
phone:          +1-555-0100
e-mail:         noc@example.net
nic-hdl:        NOC-AP
changed:        contributor@example.net 20240101
source:         APNIC
"""

OLD_ROUTE = """\
route:          192.0.2.0/24
descr:          old description
origin:         AS64496
admin-c:        NOC-AP
tech-c:         NOC-AP
changed:        contributor@example.net 20240101
source:         MDNIC
"""

NEW_ROUTE = OLD_ROUTE.replace("old description", "new description")


def transformed(text: str, date: str = DATE) -> str:
    return transform_for_radb(parse_text(text), EMAIL, date, MAINTAINER)


def with_maintainer(text: str, maintainer: str) -> str:
    return text.replace("changed:", f"mnt-by:         {maintainer}\nchanged:")


class FakeClient:
    def __init__(self, initial=None):
        self.objects = dict(initial or {})
        self.calls = []

    def fetch(self, ref):
        self.calls.append(("fetch", ref))
        return self.objects.get(ref)

    def create(self, ref, body):
        self.calls.append(("create", ref, body))
        self.objects[ref] = body
        return 201

    def update(self, ref, body):
        self.calls.append(("update", ref, body))
        self.objects[ref] = body
        return 200

    def delete(self, ref, body):
        self.calls.append(("delete", ref, body))
        self.objects.pop(ref, None)
        return 200


def change(action: str, old: str | None, new: str | None):
    old_obj = parse_text(old) if old else None
    new_obj = parse_text(new) if new else None
    return action, publish._ref(new_obj or old_obj), old_obj, new_obj


class PublicationTests(unittest.TestCase):
    def publish(self, changes, client):
        return publish.publish_changes(
            changes,
            client,
            maintainer=MAINTAINER,
            email=EMAIL,
            date=DATE,
            reason=REASON,
        )

    def test_create_replaces_publication_fields_and_verifies(self):
        item = change("create", None, ROLE)
        client = FakeClient()
        outcomes = self.publish([item], client)

        self.assertEqual(outcomes[0][1], "created")
        sent = next(call[2] for call in client.calls if call[0] == "create")
        obj = parse_text(sent)
        self.assertEqual(obj.values("source"), ("RADB",))
        self.assertEqual(obj.values("changed"), (f"{EMAIL} {DATE}",))
        self.assertEqual(obj.values("mnt-by"), (MAINTAINER,))
        self.assertEqual(obj.values("remarks"), (RADB_REVIEWED_DESCRIPTION,))
        self.assertEqual(obj.values("descr"), ())
        self.assertNotIn("contributor@example.net", sent)
        self.assertGreaterEqual([call[0] for call in client.calls].count("fetch"), 2)

    def test_post_write_verification_accepts_radb_generated_metadata(self):
        item = change("create", None, ROLE)

        class RadbNormalizingClient(FakeClient):
            def create(self, ref, body):
                self.calls.append(("create", ref, body))
                normalized = body.replace(
                    f"changed:       {EMAIL} {DATE}\n",
                    f"changed:       {EMAIL} {DATE} # 1921 GMT\n",
                )
                normalized += "last-modified:  2026-08-01T19:21:33Z\n"
                self.objects[ref] = normalized
                return 200

        client = RadbNormalizingClient()

        self.assertEqual(self.publish([item], client)[0][1], "created")

    def test_update_checks_base_then_is_idempotent(self):
        item = change("update", OLD_ROUTE, NEW_ROUTE)
        ref = item[1]
        client = FakeClient({ref: transformed(OLD_ROUTE, "20231231")})

        self.assertEqual(self.publish([item], client)[0][1], "updated")
        writes = len([call for call in client.calls if call[0] == "update"])
        self.assertEqual(self.publish([item], client)[0][1], "already-current")
        self.assertEqual(len([call for call in client.calls if call[0] == "update"]), writes)

    def test_update_adds_review_marker_to_legacy_remote(self):
        item = change("update", OLD_ROUTE, NEW_ROUTE)
        marker = f"{'descr:':<16}{RADB_REVIEWED_DESCRIPTION}\n"
        legacy_remote = transformed(OLD_ROUTE, "20231231").replace(marker, "")
        client = FakeClient({item[1]: legacy_remote})

        self.assertEqual(self.publish([item], client)[0][1], "updated")
        sent = next(call[2] for call in client.calls if call[0] == "update")
        self.assertEqual(
            parse_text(sent).values("descr").count(RADB_REVIEWED_DESCRIPTION), 1
        )

    def test_update_accepts_legacy_git_maintainer(self):
        legacy = with_maintainer(OLD_ROUTE, "LEGACY-MNT")
        item = change("update", legacy, NEW_ROUTE)
        client = FakeClient({item[1]: transformed(legacy, "20231231")})

        self.assertEqual(self.publish([item], client)[0][1], "updated")
        sent = next(call[2] for call in client.calls if call[0] == "update")
        self.assertEqual(parse_text(sent).values("mnt-by"), (MAINTAINER,))
        self.assertNotIn("LEGACY-MNT", sent)

    def test_update_and_delete_refuse_remote_core_drift(self):
        drifted = transformed(OLD_ROUTE.replace("old description", "outside edit"))
        for action, new in (("update", NEW_ROUTE), ("delete", None)):
            with self.subTest(action=action):
                item = change(action, OLD_ROUTE, new)
                client = FakeClient({item[1]: drifted})
                with self.assertRaisesRegex(publish.PublishError, "drifted"):
                    self.publish([item], client)
                self.assertFalse(any(call[0] in {"update", "delete"} for call in client.calls))

    def test_update_and_delete_refuse_remote_maintainer_drift(self):
        drifted = transformed(OLD_ROUTE).replace(MAINTAINER, "HIJACK-MNT")
        for action, new in (("update", NEW_ROUTE), ("delete", None)):
            with self.subTest(action=action):
                item = change(action, OLD_ROUTE, new)
                client = FakeClient({item[1]: drifted})
                with self.assertRaisesRegex(publish.PublishError, "drifted"):
                    self.publish([item], client)
                self.assertFalse(
                    any(call[0] in {"update", "delete"} for call in client.calls)
                )

    def test_delete_preserves_remote_object_and_appends_delete(self):
        item = change("delete", OLD_ROUTE, None)
        remote = transformed(OLD_ROUTE, "20231231")
        client = FakeClient({item[1]: remote})

        self.assertEqual(self.publish([item], client)[0][1], "deleted")
        body = next(call[2] for call in client.calls if call[0] == "delete")
        self.assertTrue(body.startswith(remote.rstrip() + "\n"))
        deletion = parse_text(body)
        self.assertEqual(deletion.values("changed"), (f"{EMAIL} 20231231",))
        self.assertEqual(deletion.values("source"), ("RADB",))
        self.assertEqual(deletion.values("mnt-by"), (MAINTAINER,))
        self.assertEqual(deletion.values("delete"), (f"{EMAIL} {REASON}",))
        self.assertEqual(
            deletion.values("descr").count(RADB_REVIEWED_DESCRIPTION), 1
        )
        self.assertIn(f"delete:         {EMAIL} {REASON}", body)
        self.assertEqual(body.count("source:"), 1)
        self.assertTrue(body.rstrip().endswith(f"delete:         {EMAIL} {REASON}"))

    def test_delete_accepts_legacy_git_maintainer_and_preserves_remote(self):
        legacy = with_maintainer(OLD_ROUTE, "LEGACY-MNT")
        item = change("delete", legacy, None)
        remote = transformed(legacy, "20231231")
        client = FakeClient({item[1]: remote})

        self.assertEqual(self.publish([item], client)[0][1], "deleted")
        body = next(call[2] for call in client.calls if call[0] == "delete")
        self.assertTrue(body.startswith(remote.rstrip() + "\n"))
        deletion = parse_text(body)
        self.assertEqual(deletion.values("mnt-by"), (MAINTAINER,))
        self.assertNotIn("LEGACY-MNT", body)

    def test_missing_delete_is_idempotent(self):
        item = change("delete", OLD_ROUTE, None)
        client = FakeClient()
        self.assertEqual(self.publish([item], client)[0][1], "already-absent")
        self.assertFalse(any(call[0] == "delete" for call in client.calls))

    def test_post_write_mismatch_fails(self):
        item = change("update", OLD_ROUTE, NEW_ROUTE)

        class StaleClient(FakeClient):
            def update(self, ref, body):
                self.calls.append(("update", ref, body))
                return 200

        client = StaleClient({item[1]: transformed(OLD_ROUTE)})
        with self.assertRaisesRegex(publish.PublishError, "post-write"):
            self.publish([item], client)

    def test_legacy_maintainer_is_replaced_by_configured_maintainer(self):
        item = change("create", None, with_maintainer(ROLE, "LEGACY-MNT"))
        client = FakeClient()

        self.assertEqual(self.publish([item], client)[0][1], "created")
        sent = next(call[2] for call in client.calls if call[0] == "create")
        self.assertEqual(parse_text(sent).values("mnt-by"), (MAINTAINER,))
        self.assertNotIn("LEGACY-MNT", sent)


class FakeResponse:
    def __init__(self, body=b"ok", status=200):
        self.body = body
        self.status = status
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, limit):
        return self.body[:limit]

    def close(self):
        self.closed = True


class RadbClientTests(unittest.TestCase):
    def test_object_paths_cover_non_route_and_route_classes(self):
        cases = {
            ("person", ("PETER-AP",)): "/person/PETER-AP",
            ("as-set", ("AS64496:AS-CUSTOMERS",)): "/as-set/AS64496%3AAS-CUSTOMERS",
            ("route", ("192.0.2.0/24", "AS64496")): "/route/192.0.2.0/24/AS64496",
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(publish._object_path(ref), expected)

    def test_fixed_https_endpoint_and_documented_auth(self):
        requests = []

        def transport(request, timeout):
            requests.append(request)
            return FakeResponse()

        client = publish.RadbClient("portal-user", "account-pass", "irr pass", transport=transport)
        ref = ("route6", ("2001:db8::/32", "AS64496"))
        client.update(ref, "route6: 2001:db8::/32\n")

        request = requests[0]
        self.assertTrue(request.full_url.startswith(
            "https://api.radb.net/api/radb/route6/2001:db8::/32/AS64496?"
        ))
        self.assertIn("password=irr+pass", request.full_url)
        self.assertTrue(request.headers["Authorization"].startswith("Basic "))
        self.assertEqual(request.get_method(), "PUT")

    def test_write_transport_failure_is_not_retried_or_leaked(self):
        calls = []

        def transport(request, timeout):
            calls.append(request)
            raise URLError("https://attacker.invalid/?password=irr-secret")

        client = publish.RadbClient("user", "account-secret", "irr-secret", transport=transport)
        with self.assertRaises(publish.PublishError) as caught:
            client.create(("role", ("NOC-AP",)), ROLE)
        rendered = str(caught.exception)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("https://", rendered)

    def test_http_error_reports_redacted_server_detail(self):
        def transport(request, timeout):
            body = (
                "Unknown attribute descr for person; "
                "portal-user account-secret irr secret irr+secret"
            ).encode()
            raise HTTPError(request.full_url, 400, "bad request", {}, io.BytesIO(body))

        client = publish.RadbClient(
            "portal-user", "account-secret", "irr secret", transport=transport
        )
        with self.assertRaises(publish.PublishError) as caught:
            client.create(("person", ("PETER-AP",)), ROLE)

        rendered = str(caught.exception)
        self.assertIn("RADB POST failed with HTTP 400", rendered)
        self.assertIn("Unknown attribute descr for person", rendered)
        self.assertNotIn("portal-user", rendered)
        self.assertNotIn("account-secret", rendered)
        self.assertNotIn("irr secret", rendered)
        self.assertNotIn("irr+secret", rendered)

    def test_read_404_means_absent(self):
        calls = []

        def transport(request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 404, "missing", {}, None)

        client = publish.RadbClient("user", "account", "irr", transport=transport)
        self.assertIsNone(client.fetch(("role", ("NOC-AP",))))
        self.assertEqual(len(calls), 1)

    def test_redirect_handler_refuses_followup(self):
        handler = publish._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://other.invalid"))


class GitChangeTests(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_builds_update_from_before_and_after_git_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Test")
            self.git(root, "config", "user.email", "test@example.net")
            path = root / "data" / "route" / "example"
            path.parent.mkdir(parents=True)
            path.write_text(OLD_ROUTE, encoding="utf-8")
            self.git(root, "add", ".")
            self.git(root, "commit", "-qm", "old")
            before = self.git(root, "rev-parse", "HEAD")
            path.write_text(NEW_ROUTE, encoding="utf-8")
            self.git(root, "commit", "-qam", "new")
            after = self.git(root, "rev-parse", "HEAD")

            changes = publish.build_changes(root, before, after)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0][:2], ("update", ("route", ("192.0.2.0/24", "AS64496"))))
            self.assertEqual(publish._exact_range(root, before, after), (before, after))

    def test_removing_only_a_legacy_maintainer_is_not_a_publication_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "Test")
            self.git(root, "config", "user.email", "test@example.net")
            path = root / "data" / "route" / "example"
            path.parent.mkdir(parents=True)
            path.write_text(
                with_maintainer(OLD_ROUTE, "LEGACY-MNT"), encoding="utf-8"
            )
            self.git(root, "add", ".")
            self.git(root, "commit", "-qm", "legacy")
            before = self.git(root, "rev-parse", "HEAD")
            path.write_text(OLD_ROUTE, encoding="utf-8")
            self.git(root, "commit", "-qam", "remove repository maintainer")
            after = self.git(root, "rev-parse", "HEAD")

            self.assertEqual(publish.build_changes(root, before, after), [])

    def test_disabled_mode_plans_without_reading_secrets_or_network(self):
        item = change("create", None, ROLE)
        variables = {
            "RADB_PUBLISH_ENABLED": "false",
            "RADB_MAINTAINER": MAINTAINER,
            "RADB_DELETE_REASON": REASON,
        }
        output = io.StringIO()
        with (
            patch.dict(os.environ, variables, clear=True),
            patch.object(publish, "_exact_range", return_value=("a", "b")),
            patch.object(publish, "build_changes", return_value=[item]),
            patch.object(publish, "git_commit_date", return_value=DATE),
            patch.object(publish, "RadbClient") as client,
            redirect_stdout(output),
        ):
            self.assertEqual(publish.main(["--before", "a", "--after", "b"]), 0)
        client.assert_not_called()
        self.assertIn("planned: create role NOC-AP", output.getvalue())

    def test_enabled_mode_uses_username_as_publication_contact(self):
        item = change("create", None, ROLE)
        variables = {
            "RADB_PUBLISH_ENABLED": "true",
            "RADB_MAINTAINER": MAINTAINER,
            "RADB_DELETE_REASON": REASON,
            "RADB_USERNAME": EMAIL,
            "RADB_ACCOUNT_PASSWORD": "account-secret",
            "RADB_IRR_PASSWORD": "maintainer-secret",
        }
        with (
            patch.dict(os.environ, variables, clear=True),
            patch.object(publish, "_exact_range", return_value=("a", "b")),
            patch.object(publish, "build_changes", return_value=[item]),
            patch.object(publish, "git_commit_date", return_value=DATE),
            patch.object(publish, "RadbClient") as client_type,
            patch.object(publish, "publish_changes", return_value=[]) as publish_changes,
        ):
            self.assertEqual(publish.main(["--before", "a", "--after", "b"]), 0)

        client_type.assert_called_once_with(EMAIL, "account-secret", "maintainer-secret")
        self.assertEqual(publish_changes.call_args.kwargs["email"], EMAIL)

    def test_exact_range_accepts_any_forward_main_update(self):
        before, after = "a" * 40, "b" * 40
        outputs = [before, after, after, before]
        with patch.object(publish, "_git", side_effect=outputs):
            self.assertEqual(publish._exact_range(Path("."), before, after), (before, after))

    def test_new_contact_and_route_update_precede_old_contact_delete(self):
        old_contact = "data/role/NOC-AP"
        new_contact = "data/role/NOC2-AP"
        route_path = "data/route/192.0.2.0_24__AS64496"
        new_role = ROLE.replace("NOC-AP", "NOC2-AP")
        new_route = NEW_ROUTE.replace("NOC-AP", "NOC2-AP")
        texts = {
            ("before", old_contact): ROLE,
            ("before", route_path): OLD_ROUTE,
            ("after", new_contact): new_role,
            ("after", route_path): new_route,
        }
        with (
            patch.object(
                publish,
                "git_changed_paths",
                return_value=[old_contact, new_contact, route_path],
            ),
            patch.object(
                publish,
                "git_file_text",
                side_effect=lambda _root, revision, path: texts.get((revision, path)),
            ),
        ):
            changes = publish.build_changes(Path("."), "before", "after")

        self.assertEqual(
            [(action, ref[0]) for action, ref, _old, _new in changes],
            [("create", "role"), ("update", "route"), ("delete", "role")],
        )


if __name__ == "__main__":
    unittest.main()
