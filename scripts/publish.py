#!/usr/bin/env python3
"""Publish one owner-approved, linear Git change to RADB."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import subprocess
import sys
import time
from typing import Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

try:  # Supports both ``python scripts/publish.py`` and test imports.
    from .rpsl import (
        RADB_REVIEWED_DESCRIPTION,
        git_changed_paths,
        git_commit_date,
        git_file_text,
        parse_text,
        transform_for_radb,
        validate_object,
    )
except ImportError:  # pragma: no cover - exercised by the workflow entry point
    from rpsl import (  # type: ignore[no-redef]
        RADB_REVIEWED_DESCRIPTION,
        git_changed_paths,
        git_commit_date,
        git_file_text,
        parse_text,
        transform_for_radb,
        validate_object,
    )


RADB_API = "https://api.radb.net/api/radb"
DATA_CLASSES = frozenset({"as-set", "person", "role", "route", "route6"})
SERVER_ATTRIBUTES = frozenset({"created", "last-modified", "rpki-ov-state", "delete"})
PUBLICATION_ATTRIBUTES = frozenset({"source", "changed"})
MAX_RESPONSE_BYTES = 1024 * 1024
UNCERTAIN_WRITE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
VERIFY_ATTEMPTS = 5
VERIFY_DELAY_SECONDS = 1.0
MAINTAINER_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,79}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class PublishError(RuntimeError):
    """A publication failure safe to print without exposing credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_transport(request: Request, timeout: float):
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    return opener.open(request, timeout=timeout)


class RadbClient:
    """Small fixed-host RADB client; writes are deliberately never retried."""

    def __init__(
        self,
        username: str,
        account_password: str,
        irr_password: str,
        *,
        transport: Callable[[Request, float], object] = _default_transport,
        timeout: float = 30.0,
    ) -> None:
        if not username or not account_password or not irr_password:
            raise PublishError("RADB credentials are incomplete")
        if any(character in username for character in "\r\n:"):
            raise PublishError("RADB username is invalid")
        self._basic = base64.b64encode(
            f"{username}:{account_password}".encode("utf-8")
        ).decode("ascii")
        self._irr_password = irr_password
        encoded_values = [
            urlencode({"value": value}).partition("=")[2]
            for value in (username, account_password, irr_password)
        ]
        self._redactions = tuple(
            dict.fromkeys(
                (
                    username,
                    account_password,
                    irr_password,
                    self._basic,
                    *encoded_values,
                )
            )
        )
        self._transport = transport
        self._timeout = timeout

    def fetch(self, ref: tuple[str, tuple[str, ...]]) -> str | None:
        response = self._request("GET", _object_path(ref), missing_ok=True)
        return None if response is None else response[1]

    def create(self, ref: tuple[str, tuple[str, ...]], body: str) -> int:
        response = self._request("POST", f"/{ref[0]}", body=body)
        assert response is not None
        return response[0]

    def update(self, ref: tuple[str, tuple[str, ...]], body: str) -> int:
        response = self._request("PUT", _object_path(ref), body=body)
        assert response is not None
        return response[0]

    def delete(self, ref: tuple[str, tuple[str, ...]], body: str) -> int:
        response = self._request("DELETE", _object_path(ref), body=body)
        assert response is not None
        return response[0]

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        missing_ok: bool = False,
    ) -> tuple[int, str] | None:
        write = method != "GET"
        query = f"?{urlencode({'password': self._irr_password})}" if write else ""
        url = f"{RADB_API}{path}{query}"
        headers = {
            "Accept": "text/plain",
            "Authorization": f"Basic {self._basic}",
            "User-Agent": "moedb/1",
        }
        payload = None if body is None else body.encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"

        request = Request(url, data=payload, headers=headers, method=method)
        try:
            response = self._transport(request, self._timeout)
            try:
                status = int(getattr(response, "status", response.getcode()))
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if len(raw) > MAX_RESPONSE_BYTES:
                raise PublishError(f"RADB {method} response was too large")
            if not 200 <= status < 300:
                detail = self._response_detail(raw)
                suffix = f": {detail}" if detail else ""
                raise PublishError(f"RADB {method} failed with HTTP {status}{suffix}")
            return status, raw.decode("utf-8", errors="replace")
        except HTTPError as error:
            status = int(error.code)
            if status == 404 and missing_ok:
                error.close()
                return None
            raw = b""
            if error.fp is not None:
                try:
                    raw = error.read(MAX_RESPONSE_BYTES + 1)
                except OSError:
                    pass
            error.close()
            suffix = "; write outcome is unknown" if write and status in UNCERTAIN_WRITE_STATUS else ""
            detail = self._response_detail(raw)
            detail_suffix = f": {detail}" if detail else ""
            raise PublishError(
                f"RADB {method} failed with HTTP {status}{suffix}{detail_suffix}"
            ) from None
        except (URLError, TimeoutError, OSError) as error:
            suffix = "; write outcome is unknown" if write else ""
            raise PublishError(f"RADB {method} failed ({type(error).__name__}){suffix}") from None

    def _response_detail(self, raw: bytes) -> str:
        if not raw or len(raw) > MAX_RESPONSE_BYTES:
            return ""
        detail = raw.decode("utf-8", errors="replace")
        for value in self._redactions:
            detail = detail.replace(value, "[REDACTED]")
        return " ".join(detail.split())[:512]


def _object_path(ref: tuple[str, tuple[str, ...]]) -> str:
    object_class, keys = ref
    if object_class not in DATA_CLASSES:
        raise PublishError("unsupported RADB object class")
    if object_class in {"route", "route6"}:
        prefix, origin = keys
        address, separator, length = prefix.rpartition("/")
        if not separator:
            raise PublishError("route key is missing a prefix length")
        return "/{}/{}/{}/{}".format(
            object_class,
            quote(address, safe=":"),
            quote(length, safe=""),
            quote(origin, safe=""),
        )
    return f"/{object_class}/{quote(keys[0], safe='')}"


def _identity(obj) -> tuple[str, ...]:
    primary_key = obj.primary_key
    if obj.object_class in {"person", "role"}:
        return "contact", primary_key[0].upper()
    if obj.object_class in {"route", "route6"}:
        return (
            obj.object_class,
            primary_key[0].lower(),
            primary_key[1].upper(),
        )
    return obj.object_class, primary_key[0].upper()


def _ref(obj) -> tuple[str, tuple[str, ...]]:
    primary_key = obj.primary_key
    if obj.object_class in {"route", "route6"}:
        keys = (primary_key[0], primary_key[1].upper())
    else:
        keys = (primary_key[0].upper(),)
    return obj.object_class, keys


def _semantic(obj, *, core: bool, repository: bool = False) -> tuple[tuple[str, str], ...]:
    ignored = set(SERVER_ATTRIBUTES)
    if core:
        ignored.update(PUBLICATION_ATTRIBUTES)
    if repository:
        ignored.add("mnt-by")
    values = []
    for entry in obj.entries:
        if entry.name in ignored:
            continue
        normalized = " ".join(entry.value.split())
        if entry.name == "changed":
            normalized = normalized.partition(" #")[0]
        if (
            core
            and entry.name in {"descr", "remarks"}
            and normalized == RADB_REVIEWED_DESCRIPTION
        ):
            continue
        values.append((entry.name, normalized))
    return tuple(sorted(values))


def _data_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        len(parts) == 3
        and parts[0] == "data"
        and parts[1] in DATA_CLASSES
        and bool(parts[2])
        and not parts[2].startswith(".")
        and not parts[2].lower().endswith(".rpsl")
    )


def build_changes(root: Path, before: str, after: str):
    paths = tuple(git_changed_paths(root, before, after))
    invalid_data = [path for path in paths if path.startswith("data/") and not _data_path(path) and not path.endswith("/.gitkeep")]
    controls = [path for path in paths if not path.startswith("data/")]
    data_paths = [path for path in paths if _data_path(path)]
    if invalid_data:
        raise PublishError("publication contains an invalid data path")
    if data_paths and controls:
        raise PublishError("publication mixes object data and control files")
    if not data_paths:
        raise PublishError("publication contains no object changes")

    before_objects = {}
    after_objects = {}
    for path in data_paths:
        for revision, destination in ((before, before_objects), (after, after_objects)):
            text = git_file_text(root, revision, path)
            if text is None:
                continue
            obj = parse_text(text, path)
            identity = _identity(obj)
            if identity in destination:
                raise PublishError("publication contains a duplicate object identity")
            destination[identity] = obj

    changes = []
    for identity in set(before_objects) | set(after_objects):
        old = before_objects.get(identity)
        new = after_objects.get(identity)
        if old is not None and new is not None and old.object_class != new.object_class:
            raise PublishError("a contact cannot change between person and role")
        if old is None:
            changes.append(("create", _ref(new), None, new))
        elif new is None:
            changes.append(("delete", _ref(old), old, None))
        elif _semantic(old, core=False, repository=True) != _semantic(
            new, core=False, repository=True
        ):
            changes.append(("update", _ref(new), old, new))

    def order(change):
        action, ref, _old, _new = change
        upsert = {"person": 10, "role": 10, "as-set": 20, "route": 30, "route6": 30}
        deleting = {"route": 10, "route6": 10, "as-set": 20, "person": 40, "role": 40}
        return (deleting if action == "delete" else upsert)[ref[0]], ref

    return sorted(changes, key=order)


def build_reconcile_change(root: Path, revision: str, path: str):
    """Build one fail-closed create/idempotency check for a current data object."""

    if not _data_path(path):
        raise PublishError("reconcile path must be one canonical data object path")
    text = git_file_text(root, revision, path)
    if text is None:
        raise PublishError("reconcile path does not exist at the checked-out revision")
    obj = parse_text(text, path)
    object_errors = validate_object(obj, path)
    if object_errors:
        raise PublishError(f"reconcile target is invalid: {object_errors[0]}")
    return "create", _ref(obj), None, obj


def _desired(obj, email: str, date: str, maintainer: str) -> str:
    rendered = transform_for_radb(obj, email, date, maintainer)
    parsed = parse_text(rendered)
    if _identity(parsed) != _identity(obj):
        raise PublishError("publication transformation changed the object identity")
    if parsed.values("source") != ("RADB",) or parsed.values("changed") != (f"{email} {date}",):
        raise PublishError("publication transformation did not replace source and changed")
    if parsed.values("mnt-by") != (maintainer,):
        raise PublishError("publication transformation did not set the maintainer")
    review_attribute = "remarks" if parsed.object_class in {"person", "role"} else "descr"
    if parsed.values(review_attribute).count(RADB_REVIEWED_DESCRIPTION) != 1:
        raise PublishError("publication transformation did not add the reviewed description")
    return rendered


def _remote(client, ref):
    text = client.fetch(ref)
    if text is None:
        return None
    try:
        obj = parse_text(text)
    except ValueError:
        raise PublishError(
            f"RADB returned invalid RPSL for {_ref_label(ref)}"
        ) from None
    if obj.values("source") != ("RADB",):
        raise PublishError(
            f"{_ref_label(ref)}: remote object source is not exactly RADB"
        )
    return obj


def _ref_label(ref: tuple[str, tuple[str, ...]]) -> str:
    return f"{ref[0]} {' / '.join(ref[1])}"


def _wait_for_state(
    client,
    ref: tuple[str, tuple[str, ...]],
    desired_obj,
    *,
    attempts: int,
    delay: float,
) -> bool:
    """Poll with GET only until RADB shows the requested final state."""

    if attempts < 1 or delay < 0:
        raise PublishError("invalid RADB verification retry settings")
    last_error: PublishError | None = None
    for attempt in range(attempts):
        try:
            remote = _remote(client, ref)
        except PublishError as error:
            last_error = error
        else:
            last_error = None
            if desired_obj is None:
                if remote is None:
                    return True
            elif remote is not None and _semantic(remote, core=False) == _semantic(
                desired_obj, core=False
            ):
                return True
        if attempt + 1 < attempts and delay:
            time.sleep(delay * (2**attempt))
    if last_error is not None:
        raise PublishError(
            f"read-back verification for {_ref_label(ref)} failed: {last_error}"
        ) from None
    return False


def _delete_body(remote, email: str, reason: str) -> str:
    """Keep the remote object unchanged and append only RADB's delete line."""

    existing = remote.render().rstrip("\r\n")
    return f"{existing}\n{'delete:':<16}{email} {reason}\n"


def publish_changes(
    changes,
    client,
    *,
    maintainer: str,
    email: str,
    date: str,
    reason: str,
    verification_attempts: int = VERIFY_ATTEMPTS,
    verification_delay: float = VERIFY_DELAY_SECONDS,
):
    outcomes = []
    for action, ref, old, new in changes:
        label = _ref_label(ref)
        remote = _remote(client, ref)
        desired = _desired(new, email, date, maintainer) if new is not None else None
        desired_obj = parse_text(desired) if desired is not None else None
        expected_old = _desired(old, email, date, maintainer) if old is not None else None
        expected_old_obj = parse_text(expected_old) if expected_old is not None else None

        if action == "create":
            if remote is not None:
                if _semantic(remote, core=False) == _semantic(desired_obj, core=False):
                    outcomes.append((ref, "already-current"))
                    continue
                raise PublishError(
                    f"{label}: object already exists in RADB with different content"
                )
            outcome = "created"
        elif action == "update":
            if remote is None:
                raise PublishError(
                    f"{label}: object is missing from RADB; refusing implicit create"
                )
            if _semantic(remote, core=False) == _semantic(desired_obj, core=False):
                outcomes.append((ref, "already-current"))
                continue
            if _semantic(remote, core=True) != _semantic(expected_old_obj, core=True):
                raise PublishError(
                    f"{label}: RADB content drifted from Git; refusing overwrite"
                )
            outcome = "updated"
        else:
            if remote is None:
                outcomes.append((ref, "already-absent"))
                continue
            if _semantic(remote, core=True) != _semantic(expected_old_obj, core=True):
                raise PublishError(
                    f"{label}: RADB content drifted from Git; refusing deletion"
                )
            delete_body = _delete_body(remote, email, reason)
            outcome = "deleted"

        try:
            if action == "create":
                client.create(ref, desired)
            elif action == "update":
                client.update(ref, desired)
            else:
                client.delete(ref, delete_body)
        except PublishError as write_error:
            try:
                confirmed = _wait_for_state(
                    client,
                    ref,
                    None if action == "delete" else desired_obj,
                    attempts=verification_attempts,
                    delay=verification_delay,
                )
            except PublishError as verification_error:
                raise PublishError(
                    f"{_ref_label(ref)}: {write_error}; state reconciliation failed: "
                    f"{verification_error}"
                ) from None
            if not confirmed:
                raise PublishError(
                    f"{_ref_label(ref)}: {write_error}; write outcome could not be confirmed"
                ) from None
            outcome += "-confirmed-after-error"
        else:
            if not _wait_for_state(
                client,
                ref,
                None if action == "delete" else desired_obj,
                attempts=verification_attempts,
                delay=verification_delay,
            ):
                operation = "delete" if action == "delete" else "write"
                raise PublishError(
                    f"post-{operation} verification for {_ref_label(ref)} did not reach "
                    f"the expected state after {verification_attempts} attempts"
                )
        outcomes.append((ref, outcome))
    return outcomes


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError):
        raise PublishError("Git revision check failed") from None
    return result.stdout.strip()


def _exact_range(root: Path, before: str, after: str) -> tuple[str, str]:
    if any(not value or value.startswith("-") or any(c.isspace() for c in value) for value in (before, after)):
        raise PublishError("invalid Git revision")
    before_sha = _git(root, "rev-parse", "--verify", f"{before}^{{commit}}")
    after_sha = _git(root, "rev-parse", "--verify", f"{after}^{{commit}}")
    if _git(root, "rev-parse", "HEAD") != after_sha:
        raise PublishError("after revision must be checked-out HEAD")
    if _git(root, "merge-base", before_sha, after_sha) != before_sha:
        raise PublishError("publication is not a forward update of main")
    return before_sha, after_sha


def _setting(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise PublishError(f"missing required setting: {name}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish an approved Git change to RADB")
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--reconcile-path")
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    if args.reconcile_path:
        if args.before or args.after:
            parser.error("--reconcile-path cannot be combined with --before or --after")
    elif not args.before or not args.after:
        parser.error("--before and --after are required for normal publication")
    try:
        enabled = _setting("RADB_PUBLISH_ENABLED").casefold()
        if enabled not in {"0", "1", "false", "true", "no", "yes"}:
            raise PublishError("RADB_PUBLISH_ENABLED must be true or false")
        maintainer = _setting("RADB_MAINTAINER")
        reason = _setting("RADB_DELETE_REASON")
        if not MAINTAINER_RE.fullmatch(maintainer):
            raise PublishError("RADB maintainer is invalid")
        if reason != reason.strip() or any(c in reason for c in "\r\n"):
            raise PublishError("RADB_DELETE_REASON must be one non-empty line")
        root = Path(args.root).resolve()
        if args.reconcile_path:
            after = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
            changes = [build_reconcile_change(root, after, args.reconcile_path)]
            publication_date = changes[0][3].first("changed").rsplit(" ", 1)[-1]
        else:
            before, after = _exact_range(root, args.before, args.after)
            changes = build_changes(root, before, after)
            publication_date = git_commit_date(root, after)
        if not changes:
            raise PublishError("publication contains no semantic object changes")
        if enabled in {"0", "false", "no"}:
            for action, ref, _old, _new in changes:
                print(f"planned: {action} {ref[0]} {' / '.join(ref[1])}")
            return 0
        username = _setting("RADB_USERNAME")
        if not EMAIL_RE.fullmatch(username):
            raise PublishError("RADB username must be the publication email address")
        client = RadbClient(
            username,
            _setting("RADB_ACCOUNT_PASSWORD"),
            _setting("RADB_IRR_PASSWORD"),
        )
        outcomes = publish_changes(
            changes,
            client,
            maintainer=maintainer,
            email=username,
            date=publication_date,
            reason=reason,
        )
        for ref, outcome in outcomes:
            print(f"{outcome}: {ref[0]} {' / '.join(ref[1])}")
        return 0
    except (PublishError, ValueError) as error:
        message = "".join(c if c.isprintable() else "?" for c in str(error))
        print(f"ERROR: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
