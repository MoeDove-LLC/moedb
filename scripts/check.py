#!/usr/bin/env python3
"""Validate the repository or one pull-request range."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    from .rpsl import (
        CONTACT_CLASSES,
        OBJECT_CLASSES,
        RPSLError,
        RPSLObject,
        git_file_text,
        git_first_contact_commit,
        git_last_change_date,
        git_pr_changed_paths,
        parse_text,
        validate_object,
        validate_repository,
    )
except ImportError:  # Direct execution: python scripts/check.py
    from rpsl import (
        CONTACT_CLASSES,
        OBJECT_CLASSES,
        RPSLError,
        RPSLObject,
        git_file_text,
        git_first_contact_commit,
        git_last_change_date,
        git_pr_changed_paths,
        parse_text,
        validate_object,
        validate_repository,
    )


RDAP_ENTITY_URLS = {
    "AFRINIC": "https://rdap.afrinic.net/rdap/entity/",
    "APNIC": "https://rdap.apnic.net/entity/",
    "ARIN": "https://rdap.arin.net/registry/entity/",
    "LACNIC": "https://rdap.lacnic.net/rdap/entity/",
    "RIPE": "https://rdap.db.ripe.net/entity/",
}
MAX_RDAP_BYTES = 512 * 1024
MAX_GITHUB_BYTES = 512 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_rdap(request: Request, timeout: float):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _open_github(request: Request, timeout: float):
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def _positive_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def github_pr_author_id(
    repository: str,
    repository_id: int,
    default_branch: str,
    commit: str,
    token: str,
    opener=None,
) -> int:
    """Resolve an integration commit to its one merged PR author's durable ID."""

    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise RPSLError("GitHub ownership context contains an invalid repository")
    if not _positive_integer(repository_id):
        raise RPSLError("GitHub ownership context contains an invalid repository ID")
    if (
        not isinstance(default_branch, str)
        or not default_branch
        or len(default_branch) > 255
        or any(character.isspace() or ord(character) < 32 for character in default_branch)
    ):
        raise RPSLError("GitHub ownership context contains an invalid default branch")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise RPSLError("GitHub ownership lookup received an invalid commit")
    if not isinstance(token, str) or not token or token.strip() != token or "\n" in token:
        raise RPSLError("GITHUB_TOKEN is required for automatic ownership lookup")

    owner, name = repository.split("/", 1)
    request = Request(
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(name, safe='')}/commits/{commit}/pulls?per_page=2",
        headers={
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {token}",
            "User-Agent": "moedb-owner-check/1",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    open_request = opener or _open_github
    try:
        with open_request(request, timeout=10) as response:
            payload = response.read(MAX_GITHUB_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise RPSLError(f"GitHub ownership lookup failed with HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise RPSLError(
            f"GitHub ownership lookup failed with {type(error).__name__}"
        ) from None
    if len(payload) > MAX_GITHUB_BYTES:
        raise RPSLError("GitHub ownership lookup returned an oversized response")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise RPSLError("GitHub ownership lookup returned invalid JSON") from None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise RPSLError("contact introduction commit must belong to exactly one merged pull request")

    pull = data[0]
    base = pull.get("base")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    user = pull.get("user")
    author_id = user.get("id") if isinstance(user, dict) else None
    if (
        pull.get("state") != "closed"
        or not pull.get("merged_at")
        or not isinstance(base, dict)
        or base.get("ref") != default_branch
        or not isinstance(base_repo, dict)
        or base_repo.get("id") != repository_id
        or not _positive_integer(author_id)
    ):
        raise RPSLError("contact introduction commit has invalid merged pull request provenance")
    return author_id


def verify_contact_at_rir(obj: RPSLObject, opener=None) -> str | None:
    """Return an error unless the contact is an exact entity at its stated RIR."""

    source = obj.first("source") or ""
    handle = obj.first("nic-hdl") or ""
    base = RDAP_ENTITY_URLS.get(source)
    if not base or not handle:
        return "contact must have a supported RIR source and nic-hdl"
    request = Request(
        base + quote(handle, safe=""),
        headers={
            "Accept": "application/rdap+json, application/json",
            "User-Agent": "moedb-rpsl-check/1",
        },
    )
    open_request = opener or _open_rdap
    try:
        with open_request(request, timeout=10) as response:
            payload = response.read(MAX_RDAP_BYTES + 1)
    except HTTPError as error:
        error.close()
        if error.code == 404:
            return f"{handle} was not found at {source}"
        return f"{source} RDAP returned HTTP {error.code} for {handle}"
    except (URLError, TimeoutError, OSError) as error:
        return f"{source} RDAP lookup failed for {handle}: {type(error).__name__}"
    if len(payload) > MAX_RDAP_BYTES:
        return f"{source} RDAP response for {handle} is too large"
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return f"{source} RDAP returned invalid JSON for {handle}"
    if not isinstance(data, dict) or data.get("objectClassName") != "entity":
        return f"{source} RDAP did not return an entity for {handle}"
    if data.get("handle") != handle:
        return f"{source} RDAP returned a different handle for {handle}"
    return None


def _is_data_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts and parts[0] == "data")


def _is_misplaced_object_path(path: str) -> bool:
    """Identify contributor object directories that are missing the data/ prefix."""

    parts = PurePosixPath(path).parts
    return bool(parts and parts[0] in OBJECT_CLASSES)


def _parse_revision_object(text: str | None, path: str) -> tuple[RPSLObject | None, list[str]]:
    if text is None:
        return None, []
    try:
        obj = parse_text(text, path)
    except RPSLError as error:
        return None, [str(error)]
    return obj, []


def _authorization_handles(obj: RPSLObject) -> set[str]:
    if obj.object_class in CONTACT_CLASSES:
        handle = obj.first("nic-hdl") or ""
        return {handle} if handle else set()
    return {handle for name in ("admin-c", "tech-c") for handle in obj.values(name) if handle}


def _unique_errors(errors: list[str]) -> list[str]:
    """Keep the first occurrence so repository and changed-file checks do not repeat it."""

    return list(dict.fromkeys(errors))


def _contact_at_revision(
    root: Path, revision: str, handle: str
) -> tuple[str, RPSLObject] | None:
    matches: list[tuple[str, RPSLObject]] = []
    for object_class in sorted(CONTACT_CLASSES):
        path = f"data/{object_class}/{handle}"
        text = git_file_text(root, revision, path)
        if text is None:
            continue
        obj = parse_text(text, path)
        if obj.object_class != object_class or obj.first("nic-hdl") != handle:
            raise RPSLError(f"{path}: contact identity does not match its canonical path")
        matches.append((path, obj))
    if len(matches) > 1:
        raise RPSLError(f"contact '{handle}' exists as both person and role")
    return matches[0] if matches else None


def check_pr(
    root: str | Path,
    before: str,
    after: str,
    *,
    verify_rir: bool = True,
    opener=None,
    rir_confirmations: list[str] | None = None,
    pr_author_id: int | None = None,
    repository: str | None = None,
    repository_id: int | None = None,
    default_branch: str | None = None,
    github_token: str | None = None,
    github_opener=None,
    owner_resolver: Callable[[str], int] | None = None,
) -> list[str]:
    """Validate repository state, submission rules, and automatic PR ownership."""

    root_path = Path(root)
    errors = validate_repository(root_path)
    try:
        changed_paths = git_pr_changed_paths(root_path, before, after)
    except RPSLError as error:
        return _unique_errors([*errors, str(error)])

    data_paths = [
        path
        for path in changed_paths
        if _is_data_path(path) and PurePosixPath(path).name != ".gitkeep"
    ]
    control_paths = [path for path in changed_paths if not _is_data_path(path)]
    if data_paths and control_paths:
        errors.append("data and control files must be submitted in separate pull requests")

    # A path such as role/EXAMPLE-RIPE is an object submission with a missing
    # data/ prefix, not an ordinary control-file change. Validate its contents
    # before the control-only early return so a misplaced object cannot receive
    # a false-green result. Deleting such a legacy path is allowed.
    for path in control_paths:
        if not _is_misplaced_object_path(path):
            continue
        try:
            new_text = git_file_text(root_path, after, path)
        except RPSLError as error:
            errors.append(str(error))
            continue
        if new_text is None:
            continue
        new_obj, object_errors = _parse_revision_object(new_text, path)
        errors.extend(object_errors)
        if new_obj is not None:
            errors.extend(validate_object(new_obj, path))

    if not data_paths:
        return _unique_errors(errors)

    authorization_handles: set[str] = set()
    submitted_contacts: set[str] = set()
    contact_classes: dict[str, set[str]] = {}
    for path in data_paths:
        if path.endswith(".rpsl"):
            suffix_error = f"{path}: object files must not use the .rpsl suffix"
            if suffix_error not in errors:
                errors.append(suffix_error)
        try:
            old_text = git_file_text(root_path, before, path)
            new_text = git_file_text(root_path, after, path)
        except RPSLError as error:
            errors.append(str(error))
            continue
        old_obj, old_errors = _parse_revision_object(old_text, path)
        new_obj, new_errors = _parse_revision_object(new_text, path)
        errors.extend(old_errors)
        errors.extend(new_errors)
        for obj in (old_obj, new_obj):
            if obj is None:
                continue
            authorization_handles.update(_authorization_handles(obj))
            if obj.object_class in CONTACT_CLASSES:
                handle = obj.first("nic-hdl") or ""
                if handle:
                    contact_classes.setdefault(handle, set()).add(obj.object_class)
        if new_obj is None:
            if old_obj is None:
                errors.append(f"{path}: changed data path contains no RPSL object")
            continue
        for object_error in validate_object(new_obj, path):
            if object_error not in errors:
                errors.append(object_error)
        if old_text == new_text:
            continue
        if new_obj.object_class in CONTACT_CLASSES:
            handle = new_obj.first("nic-hdl") or ""
            if handle:
                submitted_contacts.add(handle)
        try:
            expected_date = git_last_change_date(root_path, after, path)
        except RPSLError as error:
            errors.append(str(error))
        else:
            changed_value = new_obj.first("changed") or ""
            actual_date = changed_value.rsplit(" ", 1)[-1]
            if actual_date != expected_date:
                errors.append(
                    f"{path}: changed date must equal its last modifying commit UTC date {expected_date}"
                )

    for handle, classes in contact_classes.items():
        if len(classes) > 1:
            errors.append(f"contact '{handle}' cannot change between person and role")

    if len(authorization_handles) != 1:
        errors.append(
            "a data pull request must affect exactly one contact handle across its before and after objects"
        )
        return _unique_errors(errors)

    handle = next(iter(authorization_handles))
    if not _positive_integer(pr_author_id):
        errors.append("automatic ownership requires the pull request author's GitHub numeric ID")
        return _unique_errors(errors)
    try:
        introduction = git_first_contact_commit(root_path, before, handle)
    except RPSLError as error:
        errors.append(str(error))
        return _unique_errors(errors)

    if introduction is None:
        if handle not in submitted_contacts:
            missing_contact = f"missing local contact '{handle}'"
            if not any(missing_contact in error for error in errors):
                errors.append(
                    f"contact '{handle}' has no trusted ownership history; add or modify "
                    f"'data/person/{handle}' or 'data/role/{handle}' in this pull request "
                    "to establish ownership"
                )
            return _unique_errors(errors)
        owner_id = pr_author_id
    else:
        try:
            if owner_resolver is not None:
                owner_id = owner_resolver(introduction)
            else:
                owner_id = github_pr_author_id(
                    repository or "",
                    repository_id if repository_id is not None else 0,
                    default_branch or "",
                    introduction,
                    github_token or "",
                    github_opener,
                )
        except RPSLError as error:
            errors.append(f"contact '{handle}': {error}")
            return _unique_errors(errors)
        if not _positive_integer(owner_id):
            errors.append(f"contact '{handle}' resolved to an invalid GitHub owner ID")
            return _unique_errors(errors)

    if owner_id != pr_author_id:
        errors.append(f"pull request author is not authorized for contact '{handle}'")
        return _unique_errors(errors)

    if verify_rir:
        final_contact = None
        try:
            final_contact = _contact_at_revision(root_path, after, handle)
        except RPSLError as error:
            errors.append(str(error))
        if final_contact is not None:
            contact_path, contact = final_contact
            if not validate_object(contact, contact_path):
                rir_error = verify_contact_at_rir(contact, opener)
                if rir_error:
                    errors.append(f"{contact_path}: {rir_error}")
                elif rir_confirmations is not None:
                    confirmation = (
                        f"RIR verified: {contact.object_class} '{handle}' exists as an "
                        f"exact entity at {contact.first('source')}"
                    )
                    if confirmation not in rir_confirmations:
                        rir_confirmations.append(confirmation)
    return _unique_errors(errors)


def _pull_request_event(path: str | Path) -> dict[str, object]:
    event_path = Path(path)
    try:
        if event_path.stat().st_size > MAX_EVENT_BYTES:
            raise RPSLError("GitHub event payload is too large")
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except RPSLError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RPSLError("cannot read a valid GitHub pull request event") from None
    if not isinstance(payload, dict):
        raise RPSLError("GitHub event payload must be an object")

    pull = payload.get("pull_request")
    repository_data = payload.get("repository")
    if not isinstance(pull, dict) or not isinstance(repository_data, dict):
        raise RPSLError("GitHub event is not a pull request event")
    base = pull.get("base")
    head = pull.get("head")
    user = pull.get("user")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if not all(isinstance(value, dict) for value in (base, head, user, base_repo)):
        raise RPSLError("GitHub pull request event is missing ownership context")

    before = base.get("sha")
    after = head.get("sha")
    base_ref = base.get("ref")
    repository = repository_data.get("full_name")
    repository_id = repository_data.get("id")
    default_branch = repository_data.get("default_branch")
    pr_author_id = user.get("id")
    if not isinstance(before, str) or not _COMMIT_RE.fullmatch(before):
        raise RPSLError("GitHub pull request event has an invalid base commit")
    if not isinstance(after, str) or not _COMMIT_RE.fullmatch(after):
        raise RPSLError("GitHub pull request event has an invalid head commit")
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise RPSLError("GitHub pull request event has an invalid repository")
    if not _positive_integer(repository_id) or not _positive_integer(pr_author_id):
        raise RPSLError("GitHub pull request event has an invalid numeric ID")
    if not isinstance(default_branch, str) or not default_branch or base_ref != default_branch:
        raise RPSLError("pull requests must target the repository default branch")
    if base_repo.get("id") != repository_id or base_repo.get("full_name") != repository:
        raise RPSLError("GitHub pull request base repository does not match this repository")
    return {
        "before": before,
        "after": after,
        "pr_author_id": pr_author_id,
        "repository": repository,
        "repository_id": repository_id,
        "default_branch": default_branch,
    }


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate MOEDB RPSL data")
    parser.add_argument("--root", default=".")
    parser.add_argument("--event")
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args(argv)
    if bool(args.base) != bool(args.head):
        parser.error("--base and --head must be used together")
    if args.event and args.base:
        parser.error("--event cannot be combined with --base or --head")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _arguments(sys.argv[1:] if argv is None else argv)
    rir_confirmations: list[str] = []
    if args.event:
        try:
            context = _pull_request_event(args.event)
        except RPSLError as error:
            errors = [str(error)]
        else:
            errors = check_pr(
                args.root,
                context["before"],
                context["after"],
                rir_confirmations=rir_confirmations,
                pr_author_id=context["pr_author_id"],
                repository=context["repository"],
                repository_id=context["repository_id"],
                default_branch=context["default_branch"],
                github_token=os.environ.get("GITHUB_TOKEN"),
            )
    elif args.base:
        errors = check_pr(
            args.root,
            args.base,
            args.head,
            rir_confirmations=rir_confirmations,
        )
    else:
        errors = validate_repository(args.root)
    for error in errors:
        print(f"ERROR: {error}")
    for confirmation in rir_confirmations:
        print(f"OK: {confirmation}")
    if errors:
        return 1
    if args.event and not rir_confirmations:
        print("OK: RIR verification not applicable")
    print("OK: RPSL data passed all checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
