#!/usr/bin/env python3
"""Publish one safe, updateable GitHub PR comment for the RPSL checker."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


COMMENT_MARKER = "<!-- moedb-rpsl-check -->"
STATUS_CONTEXT = "moedb/rpsl-validation"
MAX_LOG_BYTES = 64 * 1024
MAX_LOG_CHARS = 8_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ReportError(RuntimeError):
    """A safe reporting or API error."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _open_github(request: Request, timeout: float):
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def _clean_log(raw: bytes) -> str:
    text = raw[:MAX_LOG_BYTES].decode("utf-8", errors="replace")
    text = _CONTROL_RE.sub("", text).strip()
    truncated = len(raw) > MAX_LOG_BYTES or len(text) > MAX_LOG_CHARS
    text = text[:MAX_LOG_CHARS]
    if truncated:
        text += "\n… output truncated; open the Actions run for the full log."
    return text


def encode_report(raw: bytes, exit_code: int) -> str:
    if not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        raise ReportError("check exit code must be between 0 and 255")
    payload = json.dumps(
        {"version": 1, "exit_code": exit_code, "output": _clean_log(raw)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_report(encoded: str) -> dict[str, object] | None:
    if not encoded:
        return None
    if len(encoded) > 128 * 1024:
        raise ReportError("encoded check report is too large")
    try:
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ReportError("encoded check report is invalid") from None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("exit_code"), int)
        or not 0 <= payload["exit_code"] <= 255
        or not isinstance(payload.get("output"), str)
        or len(payload["output"]) > MAX_LOG_CHARS + 100
    ):
        raise ReportError("encoded check report has an invalid schema")
    return payload


def user_suggestions(output: str) -> list[str]:
    lowered = output.lower()
    suggestions: list[str] = []
    safe_path = r"[A-Za-z0-9_./~:-]+"
    moves = re.findall(
        rf"ERROR: ({safe_path}): object must be stored at '({safe_path})'", output
    )
    for source, destination in dict.fromkeys(moves):
        suggestions.append(
            f"重命名文件：`git mv -- {source} {destination}`。"
        )

    handles = re.findall(r"missing local contact '([A-Za-z0-9_-]+)'", output)
    for handle in dict.fromkeys(handles):
        suggestions.append(
            f"在同一个 PR 中新增 `data/person/{handle}`（个人）或 "
            f"`data/role/{handle}`（角色），并确保对象的 `nic-hdl` 为 `{handle}`。"
        )

    rules = (
        (("must be stored at", "must not use the .rpsl suffix"),
         "按报错给出的规范路径重命名文件，并移除 `.txt` / `.rpsl` 等扩展名。"),
        (("missing local contact", "no trusted ownership history"),
         "在同一个 PR 中提交所引用的 `person` 或 `role` 联系人对象；新联系人必须由本 PR 引入。"),
        (("changed date must equal",),
         "把 `changed` 的日期改为该文件最后一次提交的 UTC 日期。"),
        (("dangerous attribute 'mnt-by'", "forbidden attribute 'mnt-by'",
          "publication-controlled attribute 'mnt-by'"),
         "删除投稿中的 `mnt-by`；发布流程会自动注入维护者。"),
        (("must affect exactly one contact",),
         "确保本 PR 的所有对象只关联同一个联系人 handle。"),
        (("not authorized for contact",),
         "请使用首次引入该联系人的 GitHub 账号提交，或联系管理员核对归属。"),
        (("rdap", "was not found at"),
         "核对联系人 `nic-hdl` 与 `source`，确保它能在对应 RIR 的 RDAP 中精确查询。"),
    )
    for needles, suggestion in rules:
        if any(needle in lowered for needle in needles):
            if "must be stored at" in needles and moves:
                continue
            if "missing local contact" in needles and handles:
                continue
            suggestions.append(suggestion)
    if not suggestions:
        suggestions.append("逐项修正下方错误后重新 push；机器人会更新本评论。")
    return suggestions


def build_comment(
    *, job_result: str, report: dict[str, object] | None, head_sha: str, run_url: str
) -> tuple[str, bool, str]:
    passed = (
        job_result == "success"
        and report is not None
        and report.get("exit_code") == 0
    )
    infrastructure_failure = (
        report is None
        or job_result in {"cancelled", "skipped"}
        or (job_result != "success" and report.get("exit_code") == 0)
    )
    lines = [COMMENT_MARKER, "## RPSL 自动检查", ""]
    if passed:
        lines.extend(
            [
                "✅ 检查通过，可以进入人工审核。",
                "",
                "**投稿者建议**：当前无需修改；如继续 push，此评论会自动更新。",
                "",
                "**管理员建议**：请继续核对联系人归属和业务合理性；自动检查不替代人工审核。",
            ]
        )
        description = "RPSL validation passed"
    elif infrastructure_failure:
        lines.extend(
            [
                "⚠️ 检查未能可靠完成。",
                "",
                "检查器结果与 job 状态不一致，或未能生成结果；可能是 checkout、运行环境或网络故障。",
                "",
                "**投稿者建议**：暂时无需反复修改数据，请先查看 Actions 日志。",
                "",
                "**管理员建议**：请勿合并；先重新运行工作流并排查基础设施故障。",
            ]
        )
        description = "RPSL validation did not complete"
    else:
        lines.append("❌ 检查未通过，请修正后重新 push。")
        if report is not None:
            output = str(report.get("output", ""))
            lines.extend(["", "<details open><summary>检查输出</summary>", "", "<pre>"])
            lines.append(html.escape(output))
            lines.extend(["</pre>", "", "</details>", "", "**投稿者修改建议**："])
            for suggestion in user_suggestions(output):
                lines.append(f"- {suggestion}")
            description = "RPSL validation failed"
        lines.extend([
            "",
            "**管理员建议**：请勿合并；若确认是检查器误报，应先在可信 `main` 上修复检查器并重新运行。",
        ])
    lines.extend(
        [
            "",
            f"检查提交：`{head_sha[:12]}` · [查看 Actions 运行日志]({run_url})",
        ]
    )
    return "\n".join(lines), passed, description


def _api_request(
    token: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    opener: Callable[..., object] = _open_github,
) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        "https://api.github.com" + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "moedb-pr-check/1",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with opener(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise ReportError(f"GitHub API returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise ReportError(f"GitHub API request failed with {type(error).__name__}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReportError("GitHub API response is too large")
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ReportError("GitHub API returned invalid JSON") from None


def upsert_comment(
    token: str,
    repository: str,
    pr_number: int,
    body: str,
    *,
    requester: Callable[..., object] = _api_request,
) -> None:
    owner, name = (quote(part, safe="") for part in repository.split("/", 1))
    base = f"/repos/{owner}/{name}"
    existing_id = None
    for page in range(1, 11):
        comments = requester(
            token,
            "GET",
            f"{base}/issues/{pr_number}/comments?per_page=100&page={page}",
        )
        if not isinstance(comments, list):
            raise ReportError("GitHub comments response has an invalid schema")
        for comment in comments:
            user = comment.get("user") if isinstance(comment, dict) else None
            if (
                isinstance(comment, dict)
                and COMMENT_MARKER in str(comment.get("body", ""))
                and isinstance(user, dict)
                and user.get("login") == "github-actions[bot]"
                and isinstance(comment.get("id"), int)
            ):
                existing_id = comment["id"]
                break
        if existing_id is not None or len(comments) < 100:
            break
    if existing_id is None:
        requester(token, "POST", f"{base}/issues/{pr_number}/comments", {"body": body})
    else:
        requester(token, "PATCH", f"{base}/issues/comments/{existing_id}", {"body": body})


def set_status(
    token: str,
    repository: str,
    head_sha: str,
    state: str,
    description: str,
    run_url: str,
    *,
    requester: Callable[..., object] = _api_request,
) -> None:
    if state not in {"pending", "success", "failure"}:
        raise ReportError("GitHub status state is invalid")
    owner, name = (quote(part, safe="") for part in repository.split("/", 1))
    requester(
        token,
        "POST",
        f"/repos/{owner}/{name}/statuses/{head_sha}",
        {
            "state": state,
            "context": STATUS_CONTEXT,
            "description": description,
            "target_url": run_url,
        },
    )


def get_pr_head(
    token: str,
    repository: str,
    pr_number: int,
    *,
    requester: Callable[..., object] = _api_request,
) -> str:
    owner, name = (quote(part, safe="") for part in repository.split("/", 1))
    payload = requester(
        token,
        "GET",
        f"/repos/{owner}/{name}/pulls/{pr_number}",
    )
    head = payload.get("head") if isinstance(payload, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):
        raise ReportError("GitHub pull request response has an invalid head SHA")
    return head_sha


def publish_result(
    token: str,
    repository: str,
    pr_number: int,
    event_head_sha: str,
    job_result: str,
    report: dict[str, object] | None,
    run_url: str,
    *,
    requester: Callable[..., object] = _api_request,
) -> bool:
    current_head_sha = get_pr_head(
        token, repository, pr_number, requester=requester
    )
    if current_head_sha != event_head_sha:
        print("SKIP: pull request head changed; this check run is stale")
        return False
    body, passed, description = build_comment(
        job_result=job_result,
        report=report,
        head_sha=event_head_sha,
        run_url=run_url,
    )
    set_status(
        token,
        repository,
        event_head_sha,
        "success" if passed else "failure",
        description,
        run_url,
        requester=requester,
    )
    upsert_comment(
        token, repository, pr_number, body, requester=requester
    )
    return True


def capture(args: argparse.Namespace) -> int:
    with Path(args.log).open("rb") as log:
        raw = log.read(MAX_LOG_BYTES + 1)
    encoded = encode_report(raw, args.exit_code)
    with Path(args.github_output).open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"report={encoded}\nexit_code={args.exit_code}\n")
    return 0


def _common_environment() -> tuple[str, str, str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("MOEDB_REPOSITORY", "")
    head_sha = os.environ.get("MOEDB_HEAD_SHA", "")
    run_id = os.environ.get("MOEDB_RUN_ID", "")
    if not token or token.strip() != token:
        raise ReportError("GITHUB_TOKEN is missing or invalid")
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ReportError("repository is invalid")
    if not _SHA_RE.fullmatch(head_sha):
        raise ReportError("pull request head SHA is invalid")
    if not run_id.isdigit() or int(run_id) < 1:
        raise ReportError("workflow run ID is invalid")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    return token, repository, head_sha, run_url


def prepare_from_environment() -> int:
    token, repository, head_sha, run_url = _common_environment()
    set_status(
        token,
        repository,
        head_sha,
        "pending",
        "RPSL validation is running",
        run_url,
    )
    print("OK: PR head validation marked pending")
    return 0


def publish_from_environment() -> int:
    token, repository, head_sha, run_url = _common_environment()
    pr_text = os.environ.get("MOEDB_PR_NUMBER", "")
    job_result = os.environ.get("MOEDB_CHECK_JOB_RESULT", "")
    if not pr_text.isdigit() or int(pr_text) < 1:
        raise ReportError("pull request number is invalid")
    if job_result not in {"success", "failure", "cancelled", "skipped"}:
        raise ReportError("check job result is invalid")
    try:
        report = decode_report(os.environ.get("MOEDB_CHECK_REPORT", ""))
    except ReportError:
        report = None
    published = publish_result(
        token,
        repository,
        int(pr_text),
        head_sha,
        job_result,
        report,
        run_url,
    )
    if published:
        print("OK: PR check status and comment updated")
    return 0


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--log", required=True)
    capture_parser.add_argument("--exit-code", required=True, type=int)
    capture_parser.add_argument("--github-output", required=True)
    subparsers.add_parser("command-token")
    subparsers.add_parser("prepare")
    subparsers.add_parser("publish")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "capture":
            return capture(args)
        if args.command == "command-token":
            print(secrets.token_hex(32))
            return 0
        if args.command == "prepare":
            return prepare_from_environment()
        return publish_from_environment()
    except (OSError, ReportError) as error:
        print(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
