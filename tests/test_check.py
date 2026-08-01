from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError

from scripts.check import (
    RDAP_ENTITY_URLS,
    _pull_request_event,
    check_pr,
    github_pr_author_id,
    verify_contact_at_rir,
)
from scripts.rpsl import (
    RADB_REVIEWED_DESCRIPTION,
    RPSLError,
    git_changed_paths,
    git_commit_date,
    git_file_text,
    git_first_contact_commit,
    git_last_change_date,
    parse_text,
    transform_for_radb,
    validate_object,
    validate_repository,
)


def person(handle="JD1-RIPE", source="RIPE", date="20200102", extra=""):
    return f"""person:        Jane Doe
address:       Public business address
phone:         +1 555 0100
e-mail:        jane@example.net
nic-hdl:       {handle}
mnt-by:        MAINT-MOEDB
changed:       jane@example.net {date}
source:        {source}
{extra}"""


def role(handle="NOC1-ARIN", source="ARIN", date="20200102"):
    return f"""role:          Example NOC
address:       Public business address
phone:         +1 555 0101
e-mail:        noc@example.net
nic-hdl:       {handle}
mnt-by:        MAINT-MOEDB
changed:       noc@example.net {date}
source:        {source}
"""


def route(handle="JD1-RIPE", date="20200102", source="MDNIC"):
    return f"""route:         192.0.2.0/24
origin:        AS64496
admin-c:       {handle}
tech-c:        {handle}
mnt-by:        MAINT-MOEDB
changed:       jane@example.net {date}
source:        {source}
"""


def route6(handle="JD1-RIPE", date="20200102"):
    return f"""route6:        2001:db8::/32
origin:        AS64496
admin-c:       {handle}
tech-c:        {handle}
mnt-by:        MAINT-MOEDB
changed:       jane@example.net {date}
source:        MDNIC
"""


def as_set(handle="JD1-RIPE", date="20200102"):
    return f"""as-set:        AS64496:AS-CUSTOMERS
members:       AS64497
admin-c:       {handle}
tech-c:        {handle}
mnt-by:        MAINT-MOEDB
changed:       jane@example.net {date}
source:        MDNIC
"""


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class RPSLValidationTests(unittest.TestCase):
    def test_parser_preserves_entries_and_continuations(self):
        obj = parse_text(as_set().replace("members:       AS64497", "members:       AS64497,\n               AS64498"))
        self.assertEqual(obj.object_class, "as-set")
        self.assertEqual(obj.first("members"), "AS64497, AS64498")
        self.assertEqual(obj.primary_key, ("AS64496:AS-CUSTOMERS",))

    def test_parser_rejects_multiple_objects_and_uppercase_attributes(self):
        with self.assertRaisesRegex(RPSLError, "only one object"):
            parse_text(person() + "\nrole: other\n")
        with self.assertRaisesRegex(RPSLError, "lowercase"):
            parse_text(person().replace("person:", "Person:", 1))
        smuggled = parse_text(person().rstrip() + "\nroute: 192.0.2.0/24\n")
        self.assertTrue(any("another object class 'route'" in error for error in validate_object(smuggled)))

    def test_required_and_single_attributes(self):
        text = person().replace("phone:         +1 555 0100\n", "")
        errors = validate_object(parse_text(text))
        self.assertTrue(any("missing required attribute 'phone'" in error for error in errors))
        duplicated = person() + "source: RIPE\n"
        errors = validate_object(parse_text(duplicated))
        self.assertTrue(any("'source' must occur once" in error for error in errors))

    def test_dangerous_attributes_are_forbidden(self):
        for attribute in ("auth", "owner", "override", "api-key", "created", "last-modified"):
            with self.subTest(attribute=attribute):
                obj = parse_text(person(extra=f"{attribute}: value\n"))
                self.assertTrue(
                    any(
                        f"dangerous attribute '{attribute}'" in error
                        for error in validate_object(obj)
                    )
                )
                with self.assertRaisesRegex(RPSLError, "cannot publish forbidden attribute"):
                    transform_for_radb(obj, "publish@example.net", "20200103")

    def test_source_rules(self):
        self.assertFalse(validate_object(parse_text(person(source="APNIC"))))
        self.assertTrue(any("contact source" in error for error in validate_object(parse_text(person(source="MDNIC")))))
        self.assertTrue(any("source must be MDNIC" in error for error in validate_object(parse_text(route(source="RIPE")))))

    def test_as_set_hierarchy_uses_valid_32_bit_asns(self):
        invalid = as_set().replace("AS64496:AS-CUSTOMERS", "AS9999999999:AS-CUSTOMERS")
        self.assertTrue(any("invalid canonical as-set" in error for error in validate_object(parse_text(invalid))))

    def test_changed_date_must_be_real_and_not_future(self):
        invalid = validate_object(parse_text(person(date="20230229")))
        self.assertTrue(any("invalid date" in error for error in invalid))
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y%m%d")
        future = validate_object(parse_text(person(date=tomorrow)))
        self.assertTrue(any("future" in error for error in future))

    def test_canonical_path_and_maintainer(self):
        obj = parse_text(route())
        good = "data/route/192.0.2.0_24__AS64496"
        self.assertFalse(validate_object(obj, good, "MAINT-MOEDB"))
        errors = validate_object(obj, good + ".rpsl", "OTHER-MNT")
        self.assertTrue(any("object must be stored" in error for error in errors))
        self.assertTrue(any("must not use the .rpsl" in error for error in errors))
        self.assertTrue(any("mnt-by must be exactly" in error for error in errors))

    def test_transform_adds_review_marker_and_replaces_publication_fields(self):
        for factory in (person, role, route, route6, as_set):
            with self.subTest(object_class=factory.__name__):
                transformed = transform_for_radb(
                    parse_text(factory()), "publish@example.net", "20200103"
                )
                published = parse_text(transformed)
                self.assertEqual(transformed.count("changed:"), 1)
                self.assertEqual(transformed.count("source:"), 1)
                self.assertEqual(
                    published.values("descr").count(RADB_REVIEWED_DESCRIPTION), 1
                )
                self.assertIn("changed:       publish@example.net 20200103", transformed)
                self.assertIn("source:        RADB", transformed)
                self.assertIn("mnt-by:        MAINT-MOEDB", transformed)

        already_marked = route().replace(
            "origin:", f"descr:         {RADB_REVIEWED_DESCRIPTION}\norigin:"
        )
        published = parse_text(
            transform_for_radb(parse_text(already_marked), "publish@example.net", "20200103")
        )
        self.assertEqual(published.values("descr").count(RADB_REVIEWED_DESCRIPTION), 1)


class RepositoryValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("as-set", "person", "role", "route", "route6"):
            (self.root / "data" / name).mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_valid_repository_and_local_reference(self):
        self.write("data/person/JD1-RIPE", person())
        self.write("data/route/192.0.2.0_24__AS64496", route())
        self.assertEqual(validate_repository(self.root, "MAINT-MOEDB"), [])

    def test_admin_and_tech_must_be_the_same_local_contact(self):
        self.write("data/person/JD1-RIPE", person())
        self.write("data/role/NOC1-RIPE", role(handle="NOC1-RIPE", source="RIPE"))
        self.write(
            "data/route/192.0.2.0_24__AS64496",
            route().replace("tech-c:        JD1-RIPE", "tech-c:        NOC1-RIPE"),
        )
        errors = validate_repository(self.root)
        self.assertTrue(any("must reference the same contact" in error for error in errors))

    def test_missing_contact_reference_is_rejected(self):
        self.write("data/route/192.0.2.0_24__AS64496", route())
        errors = validate_repository(self.root)
        self.assertTrue(any("missing local contact 'JD1-RIPE'" in error for error in errors))

    def test_duplicate_contact_handle_across_person_and_role(self):
        self.write("data/person/JD1-RIPE", person())
        self.write("data/role/JD1-RIPE", role(handle="JD1-RIPE", source="RIPE"))
        errors = validate_repository(self.root)
        self.assertTrue(any("duplicate contact handle" in error for error in errors))

    def test_nested_and_unknown_data_paths_are_rejected(self):
        self.write("data/person/nested/JD1-RIPE", person())
        self.write("data/unknown/object", person())
        errors = validate_repository(self.root)
        self.assertGreaterEqual(sum("only direct object files" in error for error in errors), 2)

    def test_gitkeep_must_be_empty(self):
        self.write("data/person/.gitkeep", "not object data")
        errors = validate_repository(self.root)
        self.assertTrue(any(".gitkeep must be an empty regular file" in error for error in errors))


class GitRepositoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Tests")
        self.git("config", "user.email", "tests@example.net")
        for name in ("as-set", "person", "role", "route", "route6"):
            directory = self.root / "data" / name
            directory.mkdir(parents=True)
            (directory / ".gitkeep").write_text("", encoding="utf-8")
        self.commit("initial", "2020-01-01T12:00:00+00:00")
        self.base = self.rev()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, env=None):
        result = subprocess.run(
            ["git", *args], cwd=self.root, text=True, capture_output=True, env=env
        )
        if result.returncode:
            self.fail(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    def commit(self, message, date):
        self.git("add", "-A")
        env = os.environ.copy()
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)

    def rev(self):
        return self.git("rev-parse", "HEAD")

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def valid_submission(self):
        self.write("data/person/JD1-RIPE", person())
        self.write("data/route/192.0.2.0_24__AS64496", route())
        self.commit("data", "2020-01-02T12:00:00+00:00")
        return self.rev()

    def valid_role_submission(self):
        self.write("data/role/NOC1-ARIN", role())
        self.write("data/route/192.0.2.0_24__AS64496", route(handle="NOC1-ARIN"))
        self.commit("role data", "2020-01-02T12:00:00+00:00")
        return self.rev()


class PullRequestTests(GitRepositoryCase):
    def check(self, before, after, *, author=1001, owner=1001):
        return check_pr(
            self.root,
            before,
            after,
            verify_rir=False,
            pr_author_id=author,
            owner_resolver=lambda _commit: owner,
        )

    def test_valid_submission_and_git_helpers(self):
        head = self.valid_submission()
        self.assertEqual(self.check(self.base, head), [])
        paths = git_changed_paths(self.root, self.base, head)
        self.assertIn("data/person/JD1-RIPE", paths)
        self.assertEqual(git_last_change_date(self.root, head, "data/person/JD1-RIPE"), "20200102")
        self.assertEqual(git_commit_date(self.root, head), "20200102")
        self.assertEqual(git_first_contact_commit(self.root, head, "JD1-RIPE"), head)
        self.assertIn("person:", git_file_text(self.root, head, "data/person/JD1-RIPE"))
        self.assertIsNone(git_file_text(self.root, self.base, "data/person/JD1-RIPE"))

    def test_new_role_with_route_is_authorized_automatically(self):
        head = self.valid_role_submission()
        self.assertEqual(self.check(self.base, head, author=42, owner=999), [])

    def test_as_set_and_route6_share_one_automatic_owner(self):
        self.write("data/person/JD1-RIPE", person())
        self.write("data/as-set/AS64496~AS-CUSTOMERS", as_set())
        self.write("data/route6/2001~db8~~_32__AS64496", route6())
        self.commit("IPv6 and set", "2020-01-02T12:00:00+00:00")
        self.assertEqual(self.check(self.base, self.rev()), [])

    def test_data_and_control_changes_must_be_separate(self):
        head = self.valid_submission()
        (self.root / "README.md").write_text("control", encoding="utf-8")
        self.commit("mixed", "2020-01-02T13:00:00+00:00")
        head = self.rev()
        errors = self.check(self.base, head)
        self.assertTrue(any("separate pull requests" in error for error in errors))

    def test_exactly_one_contact_handle(self):
        self.write("data/person/JD1-RIPE", person())
        self.write("data/role/NOC1-ARIN", role())
        self.commit("two contacts", "2020-01-02T12:00:00+00:00")
        errors = self.check(self.base, self.rev())
        self.assertTrue(any("exactly one contact handle" in error for error in errors))

    def test_noncontact_cannot_use_a_different_contact(self):
        self.write("data/person/JD1-RIPE", person())
        self.write("data/route/192.0.2.0_24__AS64496", route(handle="OTHER-RIPE"))
        self.commit("wrong ref", "2020-01-02T12:00:00+00:00")
        errors = self.check(self.base, self.rev())
        self.assertTrue(any("exactly one contact handle" in error for error in errors))

    def test_changed_date_must_match_last_commit_utc_date(self):
        self.write("data/person/JD1-RIPE", person(date="20200101"))
        self.commit("wrong date", "2020-01-02T23:00:00+00:00")
        errors = self.check(self.base, self.rev())
        self.assertTrue(any("last modifying commit UTC date 20200102" in error for error in errors))

    def test_owner_can_delete_an_object_without_changing_contact(self):
        head = self.valid_submission()
        self.base = head
        (self.root / "data/route/192.0.2.0_24__AS64496").unlink()
        self.commit("delete", "2020-01-03T12:00:00+00:00")
        self.assertEqual(self.check(self.base, self.rev()), [])

    def test_other_account_cannot_delete_owned_object(self):
        self.base = self.valid_submission()
        (self.root / "data/route/192.0.2.0_24__AS64496").unlink()
        self.commit("unauthorized delete", "2020-01-03T12:00:00+00:00")
        errors = self.check(self.base, self.rev(), author=99, owner=42)
        self.assertTrue(any("not authorized" in error for error in errors))

    def test_gitkeep_can_be_removed_when_adding_an_object(self):
        (self.root / "data/person/.gitkeep").unlink()
        self.write("data/person/JD1-RIPE", person())
        self.commit("add contact", "2020-01-02T12:00:00+00:00")
        self.assertEqual(self.check(self.base, self.rev()), [])

    def test_contact_cannot_switch_between_person_and_role(self):
        self.base = self.valid_submission()
        (self.root / "data/person/JD1-RIPE").unlink()
        self.write(
            "data/role/JD1-RIPE",
            role(handle="JD1-RIPE", source="RIPE", date="20200103"),
        )
        self.commit("change contact class", "2020-01-03T12:00:00+00:00")
        errors = self.check(self.base, self.rev())
        self.assertTrue(any("cannot change between person and role" in error for error in errors))

    def test_existing_contact_does_not_need_to_change(self):
        self.base = self.valid_submission()
        self.write(
            "data/route/192.0.2.0_24__AS64496",
            route(date="20200103").replace(
                "source:        MDNIC", "remarks:       Updated route\nsource:        MDNIC"
            ),
        )
        self.commit("update route only", "2020-01-03T12:00:00+00:00")
        self.assertEqual(self.check(self.base, self.rev(), author=42, owner=42), [])

    def test_unchanged_role_is_still_verified_at_its_rir(self):
        self.base = self.valid_role_submission()
        self.write(
            "data/route/192.0.2.0_24__AS64496",
            route(handle="NOC1-ARIN", date="20200103").replace(
                "source:        MDNIC", "remarks:       Updated route\nsource:        MDNIC"
            ),
        )
        self.commit("route only", "2020-01-03T12:00:00+00:00")
        seen = []

        def opener(request, timeout):
            self.assertEqual(timeout, 10)
            seen.append(request.full_url)
            return FakeResponse(
                json.dumps({"objectClassName": "entity", "handle": "NOC1-ARIN"}).encode()
            )

        errors = check_pr(
            self.root,
            self.base,
            self.rev(),
            pr_author_id=42,
            owner_resolver=lambda _commit: 42,
            opener=opener,
        )
        self.assertEqual(errors, [])
        self.assertEqual(seen, [RDAP_ENTITY_URLS["ARIN"] + "NOC1-ARIN"])

    def test_existing_owner_is_resolved_automatically_from_github(self):
        self.base = self.valid_submission()
        introduction = self.base
        self.write(
            "data/route/192.0.2.0_24__AS64496",
            route(date="20200103").replace(
                "source:        MDNIC", "remarks:       Automatic owner\nsource:        MDNIC"
            ),
        )
        self.commit("automatic lookup", "2020-01-03T12:00:00+00:00")
        seen = []
        payload = [
            {
                "state": "closed",
                "merged_at": "2020-01-02T12:00:00Z",
                "base": {"ref": "main", "repo": {"id": 123}},
                "user": {"id": 42},
            }
        ]

        def github_opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse(json.dumps(payload).encode())

        errors = check_pr(
            self.root,
            self.base,
            self.rev(),
            verify_rir=False,
            pr_author_id=42,
            repository="MoeDove-LLC/moedb",
            repository_id=123,
            default_branch="main",
            github_token="automatic-token",
            github_opener=github_opener,
        )
        self.assertEqual(errors, [])
        self.assertIn(f"/commits/{introduction}/pulls", seen[0])

    def test_other_github_account_cannot_modify_owned_object(self):
        self.base = self.valid_submission()
        self.write(
            "data/route/192.0.2.0_24__AS64496",
            route(date="20200103").replace(
                "source:        MDNIC", "remarks:       Unauthorized\nsource:        MDNIC"
            ),
        )
        self.commit("unauthorized", "2020-01-03T12:00:00+00:00")
        errors = self.check(self.base, self.rev(), author=99, owner=42)
        self.assertTrue(any("not authorized" in error for error in errors))

    def test_numeric_id_survives_github_username_change(self):
        self.base = self.valid_submission()
        self.write("data/person/JD1-RIPE", person(date="20200103"))
        self.commit("same account new login", "2020-01-03T12:00:00+00:00")
        self.assertEqual(self.check(self.base, self.rev(), author=42, owner=42), [])

    def test_contact_transfer_in_one_pr_is_rejected(self):
        self.base = self.valid_submission()
        self.write("data/role/NOC1-ARIN", role(date="20200103"))
        self.write("data/route/192.0.2.0_24__AS64496", route(handle="NOC1-ARIN", date="20200103"))
        self.commit("attempt transfer", "2020-01-03T12:00:00+00:00")
        errors = self.check(self.base, self.rev())
        self.assertTrue(any("exactly one contact handle" in error for error in errors))

    def test_owner_can_delete_contact_and_all_references(self):
        self.base = self.valid_submission()
        (self.root / "data/person/JD1-RIPE").unlink()
        (self.root / "data/route/192.0.2.0_24__AS64496").unlink()
        self.commit("delete owned objects", "2020-01-03T12:00:00+00:00")
        self.assertEqual(self.check(self.base, self.rev(), author=42, owner=42), [])

    def test_contact_cannot_be_deleted_while_referenced(self):
        self.base = self.valid_submission()
        (self.root / "data/person/JD1-RIPE").unlink()
        self.commit("delete referenced contact", "2020-01-03T12:00:00+00:00")
        errors = self.check(self.base, self.rev(), author=42, owner=42)
        self.assertTrue(any("references missing local contact" in error for error in errors))

    def test_recreating_deleted_handle_keeps_original_owner(self):
        self.base = self.valid_submission()
        (self.root / "data/person/JD1-RIPE").unlink()
        (self.root / "data/route/192.0.2.0_24__AS64496").unlink()
        self.commit("delete", "2020-01-03T12:00:00+00:00")
        deleted = self.rev()
        self.write("data/person/JD1-RIPE", person(date="20200104"))
        self.commit("attempt reclaim", "2020-01-04T12:00:00+00:00")
        errors = self.check(deleted, self.rev(), author=99, owner=42)
        self.assertTrue(any("not authorized" in error for error in errors))

    def test_missing_pr_author_id_fails_closed(self):
        head = self.valid_submission()
        errors = check_pr(self.root, self.base, head, verify_rir=False)
        self.assertTrue(any("GitHub numeric ID" in error for error in errors))

    def test_first_contact_commit_is_the_mainline_merge_commit(self):
        main_branch = self.git("branch", "--show-current")
        self.git("checkout", "-q", "-b", "contributor")
        self.write("data/person/JD1-RIPE", person())
        self.commit("topic contact", "2020-01-02T12:00:00+00:00")
        topic_commit = self.rev()
        self.git("checkout", "-q", main_branch)
        self.git("merge", "-q", "--no-ff", "contributor", "-m", "merge contact PR")
        merge_commit = self.rev()
        self.assertNotEqual(topic_commit, merge_commit)
        self.assertEqual(
            git_first_contact_commit(self.root, merge_commit, "JD1-RIPE"), merge_commit
        )


class GitHubOwnershipTests(unittest.TestCase):
    def response(self, author_id=4242, repository_id=123):
        return [
            {
                "state": "closed",
                "merged_at": "2020-01-02T12:00:00Z",
                "base": {"ref": "main", "repo": {"id": repository_id}},
                "user": {"id": author_id},
            }
        ]

    def test_merged_pr_author_numeric_id_is_used(self):
        seen = []

        def opener(request, timeout):
            seen.append(request)
            self.assertEqual(timeout, 10)
            return FakeResponse(json.dumps(self.response()).encode())

        author_id = github_pr_author_id(
            "MoeDove-LLC/moedb", 123, "main", "a" * 40, "automatic-token", opener
        )
        self.assertEqual(author_id, 4242)
        self.assertEqual(
            seen[0].full_url,
            "https://api.github.com/repos/MoeDove-LLC/moedb/commits/"
            + "a" * 40
            + "/pulls?per_page=2",
        )
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer automatic-token")
        self.assertEqual(seen[0].get_header("X-github-api-version"), "2026-03-10")

    def test_ambiguous_or_missing_merged_pr_fails_closed(self):
        for payload in ([], self.response() * 2):
            with self.subTest(count=len(payload)):
                with self.assertRaisesRegex(RPSLError, "exactly one merged pull request"):
                    github_pr_author_id(
                        "MoeDove-LLC/moedb",
                        123,
                        "main",
                        "a" * 40,
                        "automatic-token",
                        lambda _request, timeout: FakeResponse(json.dumps(payload).encode()),
                    )

    def test_wrong_repository_provenance_is_rejected(self):
        payload = self.response(repository_id=999)
        with self.assertRaisesRegex(RPSLError, "invalid merged pull request provenance"):
            github_pr_author_id(
                "MoeDove-LLC/moedb",
                123,
                "main",
                "a" * 40,
                "automatic-token",
                lambda _request, timeout: FakeResponse(json.dumps(payload).encode()),
            )

    def test_redirect_is_rejected_without_leaking_token(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 302, "Found", {"Location": "https://example.net"}, None)

        with self.assertRaisesRegex(RPSLError, "HTTP 302") as raised:
            github_pr_author_id(
                "MoeDove-LLC/moedb", 123, "main", "a" * 40, "do-not-leak", opener
            )
        self.assertNotIn("do-not-leak", str(raised.exception))


class GitHubEventTests(unittest.TestCase):
    def event(self, author_id=4242):
        return {
            "repository": {
                "id": 123,
                "full_name": "MoeDove-LLC/moedb",
                "default_branch": "main",
            },
            "pull_request": {
                "user": {"id": author_id},
                "base": {
                    "sha": "a" * 40,
                    "ref": "main",
                    "repo": {"id": 123, "full_name": "MoeDove-LLC/moedb"},
                },
                "head": {"sha": "b" * 40},
            },
        }

    def test_pr_author_id_comes_from_github_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(self.event()), encoding="utf-8")
            context = _pull_request_event(path)
        self.assertEqual(context["pr_author_id"], 4242)
        self.assertEqual(context["repository_id"], 123)

    def test_missing_numeric_author_id_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(self.event(author_id="login-name")), encoding="utf-8")
            with self.assertRaisesRegex(RPSLError, "invalid numeric ID"):
                _pull_request_event(path)


class RDAPTests(unittest.TestCase):
    def test_each_source_uses_only_its_fixed_endpoint_and_exact_handle(self):
        for source, base in RDAP_ENTITY_URLS.items():
            with self.subTest(source=source):
                handle = "TEST1-" + source
                obj = parse_text(person(handle=handle, source=source))
                seen = []

                def opener(request, timeout):
                    seen.append((request.full_url, timeout))
                    return FakeResponse(
                        json.dumps({"objectClassName": "entity", "handle": handle}).encode()
                    )

                self.assertIsNone(verify_contact_at_rir(obj, opener))
                self.assertEqual(seen, [(base + handle, 10)])

    def test_different_handle_is_not_accepted(self):
        obj = parse_text(person())

        def opener(_request, timeout):
            self.assertEqual(timeout, 10)
            return FakeResponse(json.dumps({"objectClassName": "entity", "handle": "OTHER-RIPE"}).encode())

        self.assertIn("different handle", verify_contact_at_rir(obj, opener))

    def test_redirect_to_another_rir_is_not_accepted(self):
        obj = parse_text(person(source="AFRINIC"))

        def opener(request, timeout):
            self.assertEqual(timeout, 10)
            raise HTTPError(
                request.full_url,
                301,
                "Moved Permanently",
                {"Location": "https://rdap.db.ripe.net/entity/JD1-RIPE"},
                None,
            )

        self.assertIn("HTTP 301", verify_contact_at_rir(obj, opener))


if __name__ == "__main__":
    unittest.main()
