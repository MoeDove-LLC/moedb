"""Minimal RPSL parsing, validation, and Git helpers for MOEDB."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
from pathlib import Path, PurePosixPath
import re
import subprocess


OBJECT_CLASSES = frozenset({"as-set", "person", "role", "route", "route6"})
CONTACT_CLASSES = frozenset({"person", "role"})
RIR_SOURCES = frozenset({"AFRINIC", "APNIC", "ARIN", "LACNIC", "RIPE"})
RADB_REVIEWED_DESCRIPTION = "Customer Object - Reviewed"
PUBLICATION_CONTROLLED_ATTRIBUTES = frozenset({"mnt-by"})
DANGEROUS_ATTRIBUTES = frozenset(
    {
        "api-key",
        "auth",
        "created",
        "delete",
        "last-modified",
        "override",
        "owner",
        "password",
        "rpki-ov-state",
    }
)
MAX_FILE_BYTES = 64 * 1024
MAX_LINE_LENGTH = 4096
MAX_ATTRIBUTES = 256

_ATTRIBUTE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]*(.*)$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")
_HANDLE_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9][A-Z0-9_-]{1,79}$")
_MAINTAINER_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,79}$")
_AS_SET_RE = re.compile(r"^(?:AS[1-9][0-9]{0,9}:)*AS-[A-Z0-9][A-Z0-9_-]*$")

_REQUIRED = {
    "as-set": {"as-set", "admin-c", "tech-c", "changed", "source"},
    "person": {"person", "address", "phone", "e-mail", "nic-hdl", "changed", "source"},
    "role": {"role", "address", "phone", "e-mail", "nic-hdl", "changed", "source"},
    "route": {"route", "origin", "admin-c", "tech-c", "changed", "source"},
    "route6": {"route6", "origin", "admin-c", "tech-c", "changed", "source"},
}
_SINGLE = frozenset(
    {
        "as-set",
        "person",
        "role",
        "route",
        "route6",
        "origin",
        "nic-hdl",
        "admin-c",
        "tech-c",
        "changed",
        "source",
    }
)


class RPSLError(ValueError):
    """Raised for malformed repository data."""


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    value: str
    raw_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RPSLObject:
    entries: tuple[Entry, ...]
    path: str | None = None

    @property
    def object_class(self) -> str:
        return self.entries[0].name if self.entries else ""

    def values(self, name: str) -> tuple[str, ...]:
        wanted = name.lower()
        return tuple(entry.value for entry in self.entries if entry.name == wanted)

    def first(self, name: str, default: str | None = None) -> str | None:
        values = self.values(name)
        return values[0] if values else default

    @property
    def primary_key(self) -> tuple[str, ...]:
        if self.object_class in {"person", "role"}:
            return ((self.first("nic-hdl") or ""),)
        if self.object_class in {"route", "route6"}:
            return (
                self.first(self.object_class) or "",
                self.first("origin") or "",
            )
        return ((self.first(self.object_class) or ""),)

    def render(self) -> str:
        lines = [line for entry in self.entries for line in entry.raw_lines]
        return "\n".join(lines) + "\n"


def parse_text(text: str, path: str | Path | None = None) -> RPSLObject:
    """Parse exactly one small RPSL object, preserving its attribute lines."""

    label = str(path) if path is not None else "<text>"
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise RPSLError(f"{label}: file exceeds {MAX_FILE_BYTES} bytes")
    if "\x00" in text:
        raise RPSLError(f"{label}: NUL bytes are not allowed")

    parsed: list[tuple[str, list[str], list[str]]] = []
    ended = False
    for number, line in enumerate(text.splitlines(), 1):
        if len(line) > MAX_LINE_LENGTH:
            raise RPSLError(f"{label}:{number}: line is too long")
        if not line.strip():
            if parsed:
                ended = True
            continue
        if line.startswith(("#", "%")):
            continue
        if ended:
            raise RPSLError(f"{label}:{number}: only one object is allowed per file")
        if line[0].isspace() or line.startswith("+"):
            if not parsed:
                raise RPSLError(f"{label}:{number}: continuation without an attribute")
            parsed[-1][1].append(line.lstrip("+ \t"))
            parsed[-1][2].append(line)
            continue

        match = _ATTRIBUTE_RE.fullmatch(line)
        if not match:
            raise RPSLError(f"{label}:{number}: invalid RPSL attribute line")
        raw_name, value = match.groups()
        if raw_name != raw_name.lower():
            raise RPSLError(f"{label}:{number}: attribute names must be lowercase")
        parsed.append((raw_name, [value], [line]))
        if len(parsed) > MAX_ATTRIBUTES:
            raise RPSLError(f"{label}: too many attributes")

    if not parsed:
        raise RPSLError(f"{label}: object is empty")
    entries = tuple(
        Entry(name, " ".join(part.strip() for part in values).strip(), tuple(lines))
        for name, values, lines in parsed
    )
    return RPSLObject(entries, str(path) if path is not None else None)


def parse_file(path: str | Path) -> RPSLObject:
    file_path = Path(path)
    if file_path.is_symlink() or not file_path.is_file():
        raise RPSLError(f"{file_path}: object must be a regular file")
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RPSLError(f"{file_path}: cannot read UTF-8 object: {error}") from None
    return parse_text(text, file_path)


def canonical_relative_path(obj: RPSLObject) -> str:
    object_class = obj.object_class
    if object_class in CONTACT_CLASSES:
        filename = obj.first("nic-hdl") or ""
    elif object_class == "as-set":
        filename = (obj.first("as-set") or "").replace(":", "~")
    elif object_class in {"route", "route6"}:
        prefix = (obj.first(object_class) or "").replace(":", "~").replace("/", "_")
        filename = f"{prefix}__{obj.first('origin') or ''}"
    else:
        filename = ""
    return f"data/{object_class}/{filename}"


def iter_data_files(root: str | Path = "."):
    data = Path(root) / "data"
    for object_class in sorted(OBJECT_CLASSES):
        directory = data / object_class
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.name != ".gitkeep" and (path.is_file() or path.is_symlink()):
                yield path


def _valid_asn(value: str) -> bool:
    if not re.fullmatch(r"AS[0-9]+", value):
        return False
    number = int(value[2:])
    return 1 <= number <= 4_294_967_295 and value == f"AS{number}"


def _valid_handle(value: str) -> bool:
    return bool(_HANDLE_RE.fullmatch(value))


def _valid_email(value: str) -> bool:
    return len(value) <= 254 and bool(_EMAIL_RE.fullmatch(value))


def validate_object(
    obj: RPSLObject,
    expected_path: str | Path | None = None,
) -> list[str]:
    """Return basic schema and repository-policy errors for one object."""

    errors: list[str] = []
    label = str(expected_path or obj.path or "<object>")
    object_class = obj.object_class
    if object_class not in OBJECT_CLASSES:
        return [f"{label}: unsupported object class '{object_class}'"]

    counts: dict[str, int] = {}
    for entry in obj.entries:
        counts[entry.name] = counts.get(entry.name, 0) + 1
        if not entry.value:
            errors.append(f"{label}: '{entry.name}' must not be empty")
        if entry.name in DANGEROUS_ATTRIBUTES:
            errors.append(f"{label}: dangerous attribute '{entry.name}' is forbidden")
        if entry.name in PUBLICATION_CONTROLLED_ATTRIBUTES:
            errors.append(
                f"{label}: publication-controlled attribute '{entry.name}' must be omitted"
            )
    for name in sorted(_REQUIRED[object_class]):
        if not counts.get(name):
            errors.append(f"{label}: missing required attribute '{name}'")
    for name in sorted(_SINGLE):
        if counts.get(name, 0) > 1:
            errors.append(f"{label}: attribute '{name}' must occur once")
    for name in sorted(OBJECT_CLASSES - {object_class}):
        if counts.get(name):
            errors.append(f"{label}: contains another object class '{name}'")
    if obj.entries[0].name != object_class:
        errors.append(f"{label}: the object class must be the first attribute")

    source = obj.first("source")
    if object_class in CONTACT_CLASSES:
        if source not in RIR_SOURCES:
            errors.append(f"{label}: contact source must be AFRINIC, APNIC, ARIN, LACNIC, or RIPE")
        handle = obj.first("nic-hdl") or ""
        if not _valid_handle(handle):
            errors.append(f"{label}: nic-hdl must be a canonical uppercase handle")
    elif source != "MDNIC":
        errors.append(f"{label}: {object_class} source must be MDNIC")

    for name in ("admin-c", "tech-c"):
        for handle in obj.values(name):
            if not _valid_handle(handle):
                errors.append(f"{label}: {name} must be a canonical uppercase handle")

    if object_class == "as-set":
        value = obj.first("as-set") or ""
        hierarchy = value.split(":")[:-1]
        if not _AS_SET_RE.fullmatch(value) or any(not _valid_asn(part) for part in hierarchy):
            errors.append(f"{label}: invalid canonical as-set name")
    if object_class in {"route", "route6"}:
        prefix = obj.first(object_class) or ""
        try:
            network = ipaddress.ip_network(prefix, strict=True)
        except ValueError:
            errors.append(f"{label}: invalid canonical {object_class} prefix")
        else:
            expected_version = 4 if object_class == "route" else 6
            if network.version != expected_version or str(network) != prefix:
                errors.append(f"{label}: invalid canonical {object_class} prefix")
        if not _valid_asn(obj.first("origin") or ""):
            errors.append(f"{label}: origin must be a canonical 32-bit ASN")

    changed = obj.first("changed") or ""
    match = re.fullmatch(r"(\S+) ([0-9]{8})", changed)
    if not match or not _valid_email(match.group(1)):
        errors.append(f"{label}: changed must be 'email@example.net YYYYMMDD'")
    else:
        try:
            date = datetime.strptime(match.group(2), "%Y%m%d").date()
        except ValueError:
            errors.append(f"{label}: changed contains an invalid date")
        else:
            if date > datetime.now(timezone.utc).date():
                errors.append(f"{label}: changed date must not be in the future")

    if expected_path is not None:
        actual = PurePosixPath(str(expected_path).replace("\\", "/")).as_posix()
        if actual.startswith("./"):
            actual = actual[2:]
        canonical = canonical_relative_path(obj)
        if actual != canonical:
            errors.append(f"{label}: object must be stored at '{canonical}'")
        if actual.endswith(".rpsl"):
            errors.append(f"{label}: object files must not use the .rpsl suffix")
    return errors


def validate_repository(root: str | Path = ".") -> list[str]:
    root_path = Path(root)
    data = root_path / "data"
    errors: list[str] = []
    if not data.is_dir() or data.is_symlink():
        return ["data: required data directory is missing or unsafe"]

    expected_dirs = {data / name for name in OBJECT_CLASSES}
    for directory in expected_dirs:
        if not directory.is_dir() or directory.is_symlink():
            errors.append(f"{directory.relative_to(root_path)}: required directory is missing or unsafe")
    for path in sorted(data.rglob("*"), key=lambda item: item.as_posix()):
        if path in expected_dirs:
            continue
        relative = path.relative_to(root_path).as_posix()
        parent_ok = path.parent in expected_dirs
        if path.name == ".gitkeep" and parent_ok:
            if path.is_symlink() or not path.is_file() or path.stat().st_size:
                errors.append(f"{relative}: .gitkeep must be an empty regular file")
            continue
        if path.is_dir() or not parent_ok:
            errors.append(f"{relative}: only direct object files in the five data directories are allowed")

    objects: list[RPSLObject] = []
    identities: dict[tuple[str, ...], str] = {}
    contacts: dict[str, str] = {}
    for path in iter_data_files(root_path):
        relative = path.relative_to(root_path).as_posix()
        try:
            obj = parse_file(path)
        except RPSLError as error:
            message = str(error)
            absolute_label = str(path)
            if message == absolute_label or message.startswith(absolute_label + ":"):
                message = relative + message[len(absolute_label):]
            errors.append(message)
            continue
        objects.append(obj)
        errors.extend(validate_object(obj, relative))
        identity = (obj.object_class, *obj.primary_key)
        if identity in identities:
            errors.append(f"{relative}: duplicate object identity already in '{identities[identity]}'")
        else:
            identities[identity] = relative
        if obj.object_class in CONTACT_CLASSES:
            handle = obj.first("nic-hdl") or ""
            if handle in contacts:
                errors.append(f"{relative}: duplicate contact handle already in '{contacts[handle]}'")
            else:
                contacts[handle] = relative

    for obj in objects:
        label = Path(obj.path or "<object>").relative_to(root_path).as_posix() if obj.path else "<object>"
        if obj.object_class not in CONTACT_CLASSES:
            admin, tech = obj.values("admin-c"), obj.values("tech-c")
            if admin and tech and admin != tech:
                errors.append(f"{label}: admin-c and tech-c must reference the same contact")
        references: dict[str, list[str]] = {}
        for name in ("admin-c", "tech-c"):
            for handle in obj.values(name):
                names = references.setdefault(handle, [])
                if name not in names:
                    names.append(name)
        for handle, names in references.items():
            if handle in contacts:
                continue
            attributes = " and ".join(names)
            verb = "reference" if len(names) > 1 else "references"
            errors.append(
                f"{label}: {attributes} {verb} missing local contact '{handle}'; "
                f"add 'data/person/{handle}' or 'data/role/{handle}' in this pull request"
            )
    return errors


def _git(root: str | Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )


def _safe_git_path(path: str) -> str:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path or "\x00" in path:
        raise RPSLError(f"unsafe Git path: {path!r}")
    return pure.as_posix()


def git_changed_paths(root: str | Path, before: str, after: str) -> list[str]:
    result = _git(root, ["diff", "--no-renames", "--name-only", "-z", before, after, "--"])
    if result.returncode:
        raise RPSLError("git diff failed: " + result.stderr.decode("utf-8", "replace").strip())
    return sorted({_safe_git_path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw})


def git_file_text(root: str | Path, revision: str, path: str) -> str | None:
    safe_path = _safe_git_path(path)
    result = _git(root, ["show", f"{revision}:{safe_path}"])
    if result.returncode == 0:
        try:
            return result.stdout.decode("utf-8")
        except UnicodeError:
            raise RPSLError(f"{safe_path}: Git object is not UTF-8 text") from None
    revision_check = _git(root, ["cat-file", "-e", f"{revision}^{{commit}}"])
    if revision_check.returncode:
        raise RPSLError(f"invalid Git revision: {revision}")
    return None


def _utc_date_from_timestamp(value: bytes, label: str) -> str:
    try:
        timestamp = int(value.strip())
    except ValueError:
        raise RPSLError(f"cannot determine UTC date for {label}") from None
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d")


def git_commit_date(root: str | Path, revision: str) -> str:
    result = _git(root, ["show", "-s", "--format=%ct", revision])
    if result.returncode:
        raise RPSLError(f"invalid Git revision: {revision}")
    return _utc_date_from_timestamp(result.stdout, revision)


def git_last_change_date(root: str | Path, revision: str, path: str) -> str:
    safe_path = _safe_git_path(path)
    result = _git(root, ["log", "-1", "--format=%ct", revision, "--", safe_path])
    if result.returncode or not result.stdout.strip():
        raise RPSLError(f"cannot find the last modifying commit for '{safe_path}'")
    return _utc_date_from_timestamp(result.stdout, safe_path)


def git_first_contact_commit(root: str | Path, revision: str, handle: str) -> str | None:
    """Return the first trusted-mainline commit that introduced a contact handle."""

    if not _valid_handle(handle):
        raise RPSLError("cannot search Git history for an invalid contact handle")
    shallow = _git(root, ["--no-replace-objects", "rev-parse", "--is-shallow-repository"])
    if shallow.returncode:
        raise RPSLError("cannot determine whether the Git history is complete")
    if shallow.stdout.strip() != b"false":
        raise RPSLError("automatic ownership requires a complete, non-shallow Git history")

    paths = [f"data/person/{handle}", f"data/role/{handle}"]
    result = _git(
        root,
        [
            "--no-replace-objects",
            "log",
            "--first-parent",
            "--full-history",
            "--diff-merges=first-parent",
            "--reverse",
            "--format=%H",
            "--no-patch",
            "--diff-filter=A",
            "--no-renames",
            "--root",
            revision,
            "--",
            *paths,
        ],
    )
    if result.returncode:
        raise RPSLError("cannot inspect the trusted Git history for contact ownership")
    commits = [line.decode("ascii", "strict") for line in result.stdout.splitlines() if line]
    if any(not re.fullmatch(r"[0-9a-f]{40,64}", commit) for commit in commits):
        raise RPSLError("Git returned an invalid contact introduction commit")
    return commits[0] if commits else None


def transform_for_radb(
    obj: RPSLObject, email: str, date: str, maintainer: str
) -> str:
    """Add publication-controlled fields without touching Git."""

    forbidden = sorted({entry.name for entry in obj.entries} & DANGEROUS_ATTRIBUTES)
    if forbidden:
        raise RPSLError(f"cannot publish forbidden attribute '{forbidden[0]}'")
    if not _valid_email(email):
        raise RPSLError("RADB publication contact must be a valid email address")
    if not _MAINTAINER_RE.fullmatch(maintainer):
        raise RPSLError("RADB maintainer must be a canonical maintainer name")
    try:
        parsed_date = datetime.strptime(date, "%Y%m%d").date()
    except ValueError:
        raise RPSLError("RADB publication date must be YYYYMMDD") from None
    if parsed_date > datetime.now(timezone.utc).date():
        raise RPSLError("RADB publication date must not be in the future")
    lines = [
        line
        for entry in obj.entries
        if entry.name not in {"changed", "source", "mnt-by"}
        and not (
            entry.name == "descr"
            and " ".join(entry.value.split()) == RADB_REVIEWED_DESCRIPTION
        )
        for line in entry.raw_lines
    ]
    lines.extend(
        (
            f"{'mnt-by:':<16}{maintainer}",
            f"{'descr:':<16}{RADB_REVIEWED_DESCRIPTION}",
            f"changed:       {email} {date}",
            "source:        RADB",
        )
    )
    return "\n".join(lines) + "\n"
