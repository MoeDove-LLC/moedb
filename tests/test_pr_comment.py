import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request

from scripts.pr_comment import (
    COMMENT_MARKER,
    _NoRedirect,
    _api_request,
    _open_github,
    ReportError,
    build_comment,
    capture,
    decode_report,
    encode_report,
    get_pr_head,
    main,
    prepare_from_environment,
    publish_result,
    set_status,
    upsert_comment,
    user_suggestions,
)


class PrCommentTests(unittest.TestCase):
    def test_report_round_trip_and_control_character_removal(self):
        encoded = encode_report(b"ERROR: bad\x00 path\n", 1)
        self.assertEqual(
            decode_report(encoded),
            {"version": 1, "exit_code": 1, "output": "ERROR: bad path"},
        )

    def test_invalid_report_is_rejected(self):
        encoded = base64.urlsafe_b64encode(json.dumps({"version": 2}).encode()).decode()
        with self.assertRaises(ReportError):
            decode_report(encoded)

    def test_failed_comment_escapes_output_and_gives_both_audiences_advice(self):
        report = {
            "version": 1,
            "exit_code": 1,
            "output": (
                "ERROR: <script>\n"
                "ERROR: data/person/X.txt: object must be stored at 'data/person/X'"
            ),
        }
        body, passed, description = build_comment(
            job_result="success",
            report=report,
            head_sha="a" * 40,
            run_url="https://github.com/o/r/actions/runs/1",
        )
        self.assertFalse(passed)
        self.assertEqual(description, "RPSL validation failed")
        self.assertIn(COMMENT_MARKER, body)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertIn("投稿者修改建议", body)
        self.assertIn("管理员建议", body)
        self.assertIn("git mv -- data/person/X", body)

    def test_failed_comment_still_reports_a_successful_rir_lookup(self):
        body, passed, description = build_comment(
            job_result="success",
            report={
                "version": 1,
                "exit_code": 1,
                "output": (
                    "ERROR: data/as-set/AS-FR.txt: object must be stored at "
                    "'data/as-set/AS-FR'\n"
                    "OK: RIR verified: role 'NOC1-ARIN' exists as an exact entity at ARIN"
                ),
            },
            head_sha="a" * 40,
            run_url="https://github.com/o/r/actions/runs/1",
        )
        self.assertFalse(passed)
        self.assertEqual(description, "RPSL validation failed")
        self.assertIn("本地 `role` 联系人 `NOC1-ARIN`", body)
        self.assertIn("`ARIN` RDAP", body)
        self.assertIn("git mv -- data/as-set/AS-FR.txt", body)

    def test_success_comment(self):
        body, passed, description = build_comment(
            job_result="success",
            report={
                "version": 1,
                "exit_code": 0,
                "output": (
                    "OK: RIR verified: person 'JD1-RIPE' exists as an exact entity at RIPE\n"
                    "OK: RPSL data passed all checks"
                ),
            },
            head_sha="b" * 40,
            run_url="https://github.com/o/r/actions/runs/2",
        )
        self.assertTrue(passed)
        self.assertEqual(description, "RPSL validation passed")
        self.assertIn("检查通过", body)
        self.assertIn("本地 `person` 联系人 `JD1-RIPE`", body)
        self.assertIn("`RIPE` RDAP", body)
        self.assertIn("handle 精确一致", body)
        self.assertIn("投稿者建议", body)
        self.assertIn("管理员建议", body)

    def test_success_comment_reports_role_verification(self):
        body, passed, _description = build_comment(
            job_result="success",
            report={
                "version": 1,
                "exit_code": 0,
                "output": (
                    "OK: RIR verified: role 'NOC1-ARIN' exists as an exact entity at ARIN"
                ),
            },
            head_sha="b" * 40,
            run_url="https://github.com/o/r/actions/runs/2",
        )
        self.assertTrue(passed)
        self.assertIn("本地 `role` 联系人 `NOC1-ARIN`", body)
        self.assertIn("`ARIN` RDAP", body)

    def test_success_comment_reports_when_rir_verification_is_not_applicable(self):
        body, passed, _description = build_comment(
            job_result="success",
            report={
                "version": 1,
                "exit_code": 0,
                "output": "OK: RIR verification not applicable",
            },
            head_sha="b" * 40,
            run_url="https://github.com/o/r/actions/runs/2",
        )
        self.assertTrue(passed)
        self.assertIn("没有可查询的最终 `person` / `role`", body)

    def test_missing_report_is_an_infrastructure_failure(self):
        body, passed, description = build_comment(
            job_result="failure",
            report=None,
            head_sha="c" * 40,
            run_url="https://github.com/o/r/actions/runs/3",
        )
        self.assertFalse(passed)
        self.assertEqual(description, "RPSL validation did not complete")
        self.assertIn("运行环境或网络故障", body)

    def test_successful_checker_in_failed_job_is_infrastructure_failure(self):
        body, passed, description = build_comment(
            job_result="failure",
            report={"version": 1, "exit_code": 0, "output": "OK"},
            head_sha="c" * 40,
            run_url="https://github.com/o/r/actions/runs/3",
        )
        self.assertFalse(passed)
        self.assertEqual(description, "RPSL validation did not complete")
        self.assertIn("结果与 job 状态不一致", body)
        self.assertNotIn("投稿者修改建议", body)

    def test_cancelled_job_is_an_infrastructure_failure(self):
        body, passed, description = build_comment(
            job_result="cancelled",
            report={"version": 1, "exit_code": 1, "output": "ERROR: stale"},
            head_sha="c" * 40,
            run_url="https://github.com/o/r/actions/runs/4",
        )
        self.assertFalse(passed)
        self.assertEqual(description, "RPSL validation did not complete")
        self.assertIn("检查未能可靠完成", body)
        self.assertNotIn("投稿者修改建议", body)

    def test_failed_job_with_checker_errors_is_an_infrastructure_failure(self):
        body, passed, description = build_comment(
            job_result="failure",
            report={"version": 1, "exit_code": 1, "output": "ERROR: invalid"},
            head_sha="c" * 40,
            run_url="https://github.com/o/r/actions/runs/5",
        )
        self.assertFalse(passed)
        self.assertEqual(description, "RPSL validation did not complete")
        self.assertIn("检查未能可靠完成", body)
        self.assertNotIn("投稿者修改建议", body)

    def test_suggestions_cover_reported_pr_failures(self):
        output = "\n".join(
            [
                "ERROR: data/as-set/AS-FR.txt: object must be stored at 'data/as-set/AS-FR'",
                "ERROR: data/as-set/AS-FR.txt: admin-c references missing local contact 'ZY1410-AP'",
                "ERROR: contact has no trusted ownership history",
            ]
        )
        suggestions = user_suggestions(output)
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(
            suggestions[0],
            "重命名文件：`git mv -- data/as-set/AS-FR.txt data/as-set/AS-FR`。",
        )
        self.assertIn("data/person/ZY1410-AP", suggestions[1])
        self.assertIn("data/role/ZY1410-AP", suggestions[1])

    def test_suggestions_match_combined_contact_and_publication_control_errors(self):
        suggestions = user_suggestions(
            "ERROR: admin-c and tech-c reference missing local contact 'ZY_1410-AP'\n"
            "ERROR: publication-controlled attribute 'mnt-by' must be omitted"
        )
        self.assertTrue(any("data/person/ZY_1410-AP" in item for item in suggestions))
        self.assertTrue(any("删除投稿中的 `mnt-by`" in item for item in suggestions))

    def test_suggestions_cover_misplaced_filtered_role(self):
        suggestions = user_suggestions(
            "\n".join(
                [
                    "ERROR: role/ML24477-RIPE: object must be stored at "
                    "'data/role/ML24477-RIPE'",
                    "ERROR: role/ML24477-RIPE: publication-controlled attribute "
                    "'mnt-by' must be omitted",
                    "ERROR: role/ML24477-RIPE: dangerous attribute 'created' is forbidden",
                    "ERROR: role/ML24477-RIPE: dangerous attribute "
                    "'last-modified' is forbidden",
                    "ERROR: role/ML24477-RIPE: missing required attribute 'changed'",
                    "ERROR: role/ML24477-RIPE: missing required attribute 'e-mail'",
                    "ERROR: role/ML24477-RIPE: missing required attribute 'phone'",
                    "ERROR: role/ML24477-RIPE: contact source must be AFRINIC, APNIC, "
                    "ARIN, LACNIC, or RIPE",
                ]
            )
        )

        self.assertIn(
            "重命名文件：`git mv -- role/ML24477-RIPE data/role/ML24477-RIPE`。",
            suggestions,
        )
        self.assertIn("补齐必填属性：`changed`、`e-mail`、`phone`。", suggestions)
        self.assertIn("删除禁止投稿的属性：`created`、`last-modified`。", suggestions)
        self.assertTrue(any("不要附加 `# Filtered`" in item for item in suggestions))

    def test_capture_reads_only_the_bounded_report_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "check.log"
            output = root / "github-output"
            log.write_bytes(b"x" * (64 * 1024 + 20))

            result = capture(
                type(
                    "Args",
                    (),
                    {"log": str(log), "exit_code": 1, "github_output": str(output)},
                )()
            )

            self.assertEqual(result, 0)
            values = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(values["exit_code"], "1")
            encoded = values["report"]
            report = decode_report(encoded)
            self.assertEqual(report["exit_code"], 1)
            self.assertIn("output truncated", report["output"])

    def test_github_transport_disables_proxy_and_redirects(self):
        class FakeOpener:
            def open(self, request, timeout):
                return "response"

        fake = FakeOpener()
        with patch("scripts.pr_comment.build_opener", return_value=fake) as builder:
            response = _open_github(Request("https://api.github.com/test"), 10)
        self.assertEqual(response, "response")
        handlers = builder.call_args.args
        self.assertIsInstance(handlers[0], ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], _NoRedirect)

    def test_api_redirect_is_rejected_without_leaking_token(self):
        def opener(request, timeout):
            raise HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://example.net/steal"},
                None,
            )

        with self.assertRaisesRegex(ReportError, "HTTP 302") as raised:
            _api_request("do-not-leak", "GET", "/test", opener=opener)
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_command_token_is_unpredictable_hex(self):
        with patch("builtins.print") as output:
            self.assertEqual(main(["command-token"]), 0)
            first = output.call_args.args[0]
        with patch("builtins.print") as output:
            self.assertEqual(main(["command-token"]), 0)
            second = output.call_args.args[0]
        self.assertRegex(first, r"\A[0-9a-f]{64}\Z")
        self.assertNotEqual(first, second)

    def test_prepare_posts_independent_pending_status(self):
        environment = {
            "GITHUB_TOKEN": "token",
            "MOEDB_REPOSITORY": "owner/repo",
            "MOEDB_HEAD_SHA": "a" * 40,
            "MOEDB_RUN_ID": "12",
        }
        with patch.dict(os.environ, environment, clear=True):
            with patch("scripts.pr_comment.set_status") as status:
                self.assertEqual(prepare_from_environment(), 0)
        self.assertEqual(status.call_args.args[3], "pending")
        self.assertEqual(status.call_args.args[4], "RPSL validation is running")

    def test_workflow_suspends_commands_around_untrusted_output(self):
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/check.yml"
        ).read_text(encoding="utf-8")
        stop = workflow.index("::stop-commands::%s")
        untrusted = workflow.index('tee "$RUNNER_TEMP/rpsl-check.log"')
        restore = workflow.index("printf '::%s::\\n'", stop)
        self.assertLess(stop, untrusted)
        self.assertLess(untrusted, restore)
        self.assertIn("trap 'printf", workflow)
        self.assertIn("trap - EXIT", workflow)

    def test_workflow_uses_custom_status_for_validation_result(self):
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/check.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Preserve required-check result", workflow)
        self.assertNotIn("CHECK_EXIT_CODE", workflow)
        self.assertIn("report:\n    if: always()\n    needs: [prepare, check]", workflow)
        self.assertIn("MOEDB_CHECK_JOB_RESULT: ${{ needs.check.result }}", workflow)
        self.assertIn(
            "MOEDB_CHECK_REPORT: ${{ needs.check.outputs.report }}", workflow
        )
        self.assertIn("RPSL validation found submission errors", workflow)
        self.assertIn('elif [ "$check_exit" -ne 0 ]', workflow)
        self.assertIn('exit "$check_exit"', workflow)

    def test_upsert_updates_existing_bot_comment(self):
        calls = []

        def requester(token, method, path, payload=None):
            calls.append((method, path, payload))
            if method == "GET":
                return [
                    {"id": 7, "body": COMMENT_MARKER + " old", "user": {"login": "someone"}},
                    {
                        "id": 8,
                        "body": COMMENT_MARKER + " old",
                        "user": {"login": "github-actions[bot]"},
                    },
                ]
            return {}

        upsert_comment("token", "owner/repo", 4, "new", requester=requester)
        self.assertEqual(calls[-1], ("PATCH", "/repos/owner/repo/issues/comments/8", {"body": "new"}))

    def test_upsert_creates_when_marker_is_absent(self):
        calls = []

        def requester(token, method, path, payload=None):
            calls.append((method, path, payload))
            return [] if method == "GET" else {}

        upsert_comment("token", "owner/repo", 5, "new", requester=requester)
        self.assertEqual(
            calls[-1],
            ("POST", "/repos/owner/repo/issues/5/comments", {"body": "new"}),
        )

    def test_status_uses_required_context_and_head_sha(self):
        calls = []

        def requester(token, method, path, payload=None):
            calls.append((method, path, payload))
            return {}

        set_status(
            "token",
            "owner/repo",
            "d" * 40,
            "failure",
            "failed",
            "https://github.com/owner/repo/actions/runs/9",
            requester=requester,
        )
        method, path, payload = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/repos/owner/repo/statuses/" + "d" * 40)
        self.assertEqual(payload["state"], "failure")
        self.assertEqual(payload["context"], "moedb/rpsl-validation")

    def test_get_pr_head_validates_response(self):
        def requester(token, method, path, payload=None):
            self.assertEqual(method, "GET")
            return {"head": {"sha": "e" * 40}}

        self.assertEqual(
            get_pr_head("token", "owner/repo", 7, requester=requester),
            "e" * 40,
        )

    def test_stale_run_does_not_write_status_or_comment(self):
        calls = []

        def requester(token, method, path, payload=None):
            calls.append((method, path, payload))
            return {"head": {"sha": "f" * 40}}

        published = publish_result(
            "token",
            "owner/repo",
            8,
            "e" * 40,
            "success",
            {"version": 1, "exit_code": 0, "output": "OK"},
            "https://github.com/owner/repo/actions/runs/10",
            requester=requester,
        )
        self.assertFalse(published)
        self.assertEqual(
            calls,
            [("GET", "/repos/owner/repo/pulls/8", None)],
        )

    def test_current_run_writes_final_status_before_comment(self):
        calls = []

        def requester(token, method, path, payload=None):
            calls.append((method, path, payload))
            if path == "/repos/owner/repo/pulls/9":
                return {"head": {"sha": "a" * 40}}
            if method == "GET":
                return []
            return {}

        published = publish_result(
            "token",
            "owner/repo",
            9,
            "a" * 40,
            "success",
            {"version": 1, "exit_code": 1, "output": "ERROR: invalid"},
            "https://github.com/owner/repo/actions/runs/11",
            requester=requester,
        )
        self.assertTrue(published)
        self.assertEqual(calls[0][1], "/repos/owner/repo/pulls/9")
        self.assertEqual(calls[1][1], "/repos/owner/repo/statuses/" + "a" * 40)
        self.assertEqual(calls[1][2]["state"], "failure")
        self.assertIn("/issues/9/comments", calls[2][1])


if __name__ == "__main__":
    unittest.main()
